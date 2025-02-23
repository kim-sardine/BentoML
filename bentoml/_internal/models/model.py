from __future__ import annotations

import io
import typing as t
import logging
import importlib
from sys import version_info as pyver
from typing import overload
from typing import TYPE_CHECKING
from datetime import datetime
from datetime import timezone
from collections import UserDict

import fs
import attr
import yaml
import fs.errors
import fs.mirror
import cloudpickle
from fs.base import FS
from cattr.gen import override
from cattr.gen import make_dict_unstructure_fn
from simple_di import inject
from simple_di import Provide

from ..tag import Tag
from ..store import Store
from ..store import StoreItem
from ..types import MetadataDict
from ..utils import bentoml_cattr
from ..utils import label_validator
from ..utils import metadata_validator
from ..runner import Runner
from ..runner import Runnable
from ...exceptions import NotFound
from ...exceptions import BentoMLException
from ..configuration import BENTOML_VERSION
from ..configuration.containers import BentoMLContainer

if TYPE_CHECKING:
    from ..types import AnyType
    from ..types import PathType

    class ModelSignatureDict(t.TypedDict, total=False):
        batch_dim: tuple[int, int] | int
        batchable: bool
        input_spec: tuple[AnyType] | AnyType | None
        output_spec: AnyType | None


T = t.TypeVar("T")

logger = logging.getLogger(__name__)

PYTHON_VERSION: str = f"{pyver.major}.{pyver.minor}.{pyver.micro}"
MODEL_YAML_FILENAME = "model.yaml"
CUSTOM_OBJECTS_FILENAME = "custom_objects.pkl"


if TYPE_CHECKING:
    ModelOptionsSuper = UserDict[str, t.Any]
else:
    ModelOptionsSuper = UserDict


class ModelOptions(ModelOptionsSuper):
    @classmethod
    def with_options(cls, **kwargs: t.Any) -> ModelOptions:
        return cls(**kwargs)

    @staticmethod
    def to_dict(options: ModelOptions) -> dict[str, t.Any]:
        return dict(options)


bentoml_cattr.register_structure_hook_func(
    lambda cls: issubclass(cls, ModelOptions), lambda d, cls: cls.with_options(**d)  # type: ignore
)
bentoml_cattr.register_unstructure_hook(ModelOptions, lambda v: v.to_dict(v))  # type: ignore  # pylint: disable=unnecessary-lambda # lambda required


@attr.define(repr=False, eq=False, init=False)
class Model(StoreItem):
    _tag: Tag
    __fs: FS

    _info: ModelInfo
    _custom_objects: dict[str, t.Any] | None = None

    _runnable: t.Type[Runnable] | None = attr.field(init=False, default=None)

    def __init__(
        self,
        tag: Tag,
        model_fs: FS,
        info: ModelInfo,
        custom_objects: dict[str, t.Any] | None = None,
        *,
        _internal: bool = False,
    ):
        if not _internal:
            raise BentoMLException(
                "Model cannot be instantiated directly directly; use bentoml.<framework>.save or bentoml.models.get instead"
            )

        self.__attrs_init__(tag, model_fs, info, custom_objects)  # type: ignore (no types for attrs init)

    @staticmethod
    def _export_ext() -> str:
        return "bentomodel"

    @property
    def tag(self) -> Tag:
        return self._tag

    @property
    def _fs(self) -> FS:
        return self.__fs

    @property
    def info(self) -> ModelInfo:
        return self._info

    @property
    def custom_objects(self) -> t.Dict[str, t.Any]:
        if self._custom_objects is None:
            if self._fs.isfile(CUSTOM_OBJECTS_FILENAME):
                with self._fs.open(CUSTOM_OBJECTS_FILENAME, "rb") as cofile:
                    self._custom_objects: dict[str, t.Any] | None = cloudpickle.load(
                        cofile
                    )
                    if not isinstance(self._custom_objects, dict):
                        raise ValueError("Invalid custom objects found.")
            else:
                self._custom_objects: dict[str, t.Any] | None = {}

        return self._custom_objects

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Model) and self._tag == other._tag

    def __hash__(self) -> int:
        return hash(self._tag)

    @staticmethod
    def create(
        name: str,
        *,
        module: str,
        api_version: str,
        signatures: ModelSignaturesType,
        labels: dict[str, str] | None = None,
        options: ModelOptions | None = None,
        custom_objects: dict[str, t.Any] | None = None,
        metadata: dict[str, t.Any] | None = None,
        context: ModelContext,
    ) -> Model:
        """Create a new Model instance in temporary filesystem used for serializing
        model artifacts and save to model store

        Args:
            name: model name in target model store, model version will be automatically
                generated
            module: import path of module used for saving/loading this model, e.g.
                "bentoml.tensorflow"
            labels:  user-defined labels for managing models, e.g. team=nlp, stage=dev
            options: default options for loading this model, defined by runner
                implementation, e.g. xgboost booster_params
            custom_objects: user-defined additional python objects to be saved
                alongside the model, e.g. a tokenizer instance, preprocessor function,
                model configuration json
            metadata: user-defined metadata for storing model training context
                information or model evaluation metrics, e.g. dataset version,
                training parameters, model scores
            context: Environment context managed by BentoML for loading model,
                e.g. {"framework:" "tensorflow", "framework_version": _tf_version}

        Returns:
            object: Model instance created in temporary filesystem
        """
        tag = Tag(name).make_new_version()
        labels = {} if labels is None else labels
        metadata = {} if metadata is None else metadata
        options = ModelOptions() if options is None else options

        model_fs = fs.open_fs(f"temp://bentoml_model_{name}")

        res = Model(
            tag,
            model_fs,
            ModelInfo(
                tag=tag,
                module=module,
                api_version=api_version,
                signatures=signatures,
                labels=labels,
                options=options,
                metadata=metadata,
                context=context,
            ),
            custom_objects=custom_objects,
            _internal=True,
        )

        return res

    @inject
    def save(
        self, model_store: ModelStore = Provide[BentoMLContainer.model_store]
    ) -> Model:
        self._save(model_store)

        return self

    def _save(self, model_store: ModelStore) -> Model:
        if not self.validate():
            logger.warning(f"Failed to create Model for {self.tag}, not saving.")
            raise BentoMLException("Failed to save Model because it was invalid")

        with model_store.register(self.tag) as model_path:
            out_fs = fs.open_fs(model_path, create=True, writeable=True)
            fs.mirror.mirror(self._fs, out_fs, copy_if_newer=False)
            self._fs.close()
            self.__fs = out_fs

        logger.info(f"Successfully saved {self}")
        return self

    @classmethod
    def from_fs(cls: t.Type[Model], item_fs: FS) -> Model:
        try:
            with item_fs.open(MODEL_YAML_FILENAME, "r") as model_yaml:
                info = ModelInfo.from_yaml_file(model_yaml)
        except fs.errors.ResourceNotFound:
            raise BentoMLException(
                f"Failed to load bento model because it does not contain a '{MODEL_YAML_FILENAME}'"
            )

        res = Model(tag=info.tag, model_fs=item_fs, info=info, _internal=True)
        if not res.validate():
            raise BentoMLException(
                f"Failed to load bento model because it contains an invalid '{MODEL_YAML_FILENAME}'"
            )

        return res

    @property
    def path(self) -> str:
        return self.path_of("/")

    def path_of(self, item: str) -> str:
        return self._fs.getsyspath(item)

    def flush(self):
        self._write_info()
        self._write_custom_objects()

    def _write_info(self):
        with self._fs.open(MODEL_YAML_FILENAME, "w", encoding="utf-8") as model_yaml:
            self.info.dump(t.cast(io.StringIO, model_yaml))

    def _write_custom_objects(self):
        # pickle custom_objects if it is not None and not empty
        if self.custom_objects:
            with self._fs.open(CUSTOM_OBJECTS_FILENAME, "wb") as cofile:
                cloudpickle.dump(self.custom_objects, cofile)  # type: ignore (incomplete cloudpickle types)

    @property
    def creation_time(self) -> datetime:
        return self.info.creation_time

    def validate(self):
        return self._fs.isfile(MODEL_YAML_FILENAME)

    def __str__(self):
        return f'Model(tag="{self.tag}", path="{self.path}")'

    def to_runner(
        self,
        name: str = "",
        cpu: int | None = None,
        nvidia_gpu: int | None = None,
        custom_resources: dict[str, float] | None = None,
        max_batch_size: int | None = None,
        max_latency_ms: int | None = None,
        method_configs: dict[str, dict[str, int]] | None = None,
    ) -> Runner:
        """
        TODO(chaoyu): add docstring

        Args:
            name:
            cpu:
            nvidia_gpu:
            custom_resources:
            max_batch_size:
            max_latency_ms:
            runnable_method_configs:

        Returns:

        """
        return Runner(
            self.to_runnable(),
            name=name if name != "" else self.tag.name,
            models=[self],
            cpu=cpu,
            nvidia_gpu=nvidia_gpu,
            custom_resources=custom_resources,
            max_batch_size=max_batch_size,
            max_latency_ms=max_latency_ms,
            method_configs=method_configs,
        )

    def to_runnable(self) -> t.Type[Runnable]:
        if self._runnable is None:
            module = importlib.import_module(self.info.module)
            self._runnable = module.get_runnable(self)
        return self._runnable

    def with_options(self, **kwargs: t.Any) -> Model:
        res = Model(
            self._tag,
            self._fs,
            self.info.with_options(**kwargs),
            self._custom_objects,
            _internal=True,
        )
        return res


class ModelStore(Store[Model]):
    def __init__(self, base_path: "t.Union[PathType, FS]"):
        super().__init__(base_path, Model)


@attr.frozen
class ModelContext:
    framework_name: str
    framework_versions: t.Dict[str, str]
    bentoml_version: str = attr.field(default=BENTOML_VERSION)
    python_version: str = attr.field(default=PYTHON_VERSION)

    @staticmethod
    def from_dict(data: dict[str, str | dict[str, str]] | ModelContext) -> ModelContext:
        if isinstance(data, ModelContext):
            return data
        return bentoml_cattr.structure(data, ModelContext)

    def to_dict(self: ModelContext) -> dict[str, str | dict[str, str]]:
        return bentoml_cattr.unstructure(self)  # type: ignore (incomplete cattr types)


# Remove after attrs support ForwardRef natively
attr.resolve_types(ModelContext, globals(), locals())


@attr.frozen
class ModelSignature:
    """
    A model signature represents a method on a model object that can be called.

    This information is used when creating BentoML runners for this model.

    Note that anywhere a ``ModelSignature`` is used, a ``dict`` with keys corresponding to the
    fields can be used instead. For example, instead of ``{"predict":
    ModelSignature(batchable=True)}``, one can pass ``{"predict": {"batchable": True}}``.

    Fields:
        batchable:
            Whether multiple API calls to this predict method should be batched by the BentoML
            runner.
        batch_dim:
            The dimension(s) that contain multiple data when passing to this prediction method.

            For example, if you have two inputs you want to run prediction on, ``[1, 2]`` and ``[3,
            4]``, if the array you would pass to the predict method would be ``[[1, 2], [3, 4]]``,
            then the batch dimension would be ``0``. If the array you would pass to the predict
            method would be ``[[1, 3], [2, 4]]``, then the batch dimension would be ``1``.

            If there are multiple arguments to the predict method and there is only one batch
            dimension supplied, all arguments will use that batch dimension.

            Example: .. code-block:: python
                # Save two models with `predict` method that supports taking input batches on the
                dimension 0 and the other on dimension 1: bentoml.pytorch.save_model("demo0",
                model_0, signatures={"predict": {"batchable": True, "batch_dim": 0}})
                bentoml.pytorch.save_model("demo1", model_1, signatures={"predict": {"batchable":
                True, "batch_dim": 1}})

                # if the following calls are batched, the input to the actual predict method on the
                # model.predict method would be [[1, 2], [3, 4], [5, 6]] runner0 =
                bentoml.pytorch.get("demo0:latest").to_runner() runner0.init_local()
                runner0.predict.run(np.array([[1, 2], [3, 4]])) runner0.predict.run(np.array([[5,
                6]]))

                # if the following calls are batched, the input to the actual predict method on the
                # model.predict would be [[1, 2, 5], [3, 4, 6]] runner1 =
                bentoml.pytorch.get("demo1:latest").to_runner() runner1.init_local()
                runner1.predict.run(np.array([[1, 2], [3, 4]])) runner1.predict.run(np.array([[5],
                [6]]))

            Expert API:

            The batch dimension can also be a tuple of (input batch dimension, output batch
            dimension). For example, if the predict method should have its input batched along the
            first axis and its output batched along the zeroth axis, ``batch_dim`` can be set to
            ``(1, 0)``.

        input_spec: Reserved for future use.

        output_spec: Reserved for future use.
    """

    batchable: bool = False
    batch_dim: t.Tuple[int, int] = (0, 0)
    # TODO: define input/output spec struct
    input_spec: t.Any = None
    output_spec: t.Any = None

    @staticmethod
    def from_dict(data: ModelSignatureDict) -> ModelSignature:
        if "batch_dim" in data and isinstance(data["batch_dim"], int):
            formated_data = dict(data, batch_dim=(data["batch_dim"], data["batch_dim"]))
        else:
            formated_data = data
        return bentoml_cattr.structure(formated_data, ModelSignature)

    @staticmethod
    def convert_signatures_dict(
        data: dict[str, ModelSignatureDict | ModelSignature]
    ) -> dict[str, ModelSignature]:
        return {
            k: ModelSignature.from_dict(v) if isinstance(v, dict) else v
            for k, v in data.items()
        }


# Remove after attrs support ForwardRef natively
attr.resolve_types(ModelSignature, globals(), locals())


if TYPE_CHECKING:
    ModelSignaturesType: t.TypeAlias = (
        dict[str, ModelSignature] | dict[str, ModelSignatureDict]
    )


def model_signature_encoder(model_signature: ModelSignature) -> dict[str, t.Any]:
    encoded: dict[str, t.Any] = {
        "batchable": model_signature.batchable,
    }
    # ignore batch_dim if batchable is False
    if model_signature.batchable:
        encoded["batch_dim"] = model_signature.batch_dim
    if model_signature.input_spec is not None:
        encoded["input_spec"] = model_signature.input_spec
    if model_signature.output_spec is not None:
        encoded["output_spec"] = model_signature.output_spec
    return encoded


bentoml_cattr.register_unstructure_hook(ModelSignature, model_signature_encoder)


@attr.define(repr=False, eq=False, frozen=True)
class ModelInfo:
    tag: Tag
    name: str
    version: str
    module: str
    labels: t.Dict[str, str] = attr.field(validator=label_validator)
    options: ModelOptions
    # TODO: make metadata a MetadataDict; this works around a bug in attrs
    metadata: t.Dict[str, t.Any] = attr.field(
        validator=metadata_validator, converter=dict
    )
    context: ModelContext = attr.field()
    signatures: t.Dict[str, ModelSignature] = attr.field(
        converter=ModelSignature.convert_signatures_dict
    )
    api_version: str
    creation_time: datetime

    def __init__(
        self,
        tag: Tag,
        module: str,
        labels: dict[str, str],
        options: ModelOptions,
        metadata: MetadataDict,
        context: ModelContext,
        signatures: ModelSignaturesType,
        api_version: str,
        creation_time: datetime | None = None,
    ):
        self.__attrs_init__(  # type: ignore
            tag=tag,
            name=tag.name,
            version=tag.version,
            module=module,
            labels=labels,
            options=options,
            metadata=metadata,
            context=context,
            signatures=signatures,
            api_version=api_version,
            creation_time=creation_time or datetime.now(timezone.utc),
        )
        self.validate()

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ModelInfo):
            return False

        return (
            self.tag == other.tag
            and self.module == other.module
            and self.signatures == other.signatures
            and self.labels == other.labels
            and self.options == other.options
            and self.metadata == other.metadata
            and self.context == other.context
            and self.signatures == other.signatures
            and self.api_version == other.api_version
            and self.creation_time == other.creation_time
        )

    def with_options(self, **kwargs: t.Any) -> ModelInfo:
        return ModelInfo(
            tag=self.tag,
            module=self.module,
            signatures=self.signatures,
            labels=self.labels,
            options=self.options.with_options(**kwargs),
            metadata=self.metadata,
            context=self.context,
            api_version=self.api_version,
            creation_time=self.creation_time,
        )

    def to_dict(self) -> t.Dict[str, t.Any]:
        return bentoml_cattr.unstructure(self)  # type: ignore (incomplete cattr types)

    def parse_options(self, options_class: type[ModelOptions]) -> None:
        object.__setattr__(self, "options", options_class.with_options(**self.options))

    @overload
    def dump(self, stream: io.StringIO) -> io.BytesIO:
        ...

    @overload
    def dump(self, stream: None = None) -> None:
        ...

    def dump(self, stream: io.StringIO | None = None) -> io.BytesIO | None:
        return yaml.safe_dump(self.to_dict(), stream=stream, sort_keys=False)  # type: ignore (bad yaml types)

    @staticmethod
    def from_yaml_file(stream: t.IO[t.Any]):
        try:
            yaml_content = yaml.safe_load(stream)
        except yaml.YAMLError as exc:  # pragma: no cover - simple error handling
            logger.error(exc)
            raise

        if not isinstance(yaml_content, dict):
            raise BentoMLException(f"malformed {MODEL_YAML_FILENAME}")

        yaml_content["tag"] = str(
            Tag(
                t.cast(str, yaml_content["name"]),
                t.cast(str, yaml_content["version"]),
            )
        )
        del yaml_content["name"]
        del yaml_content["version"]

        # For backwards compatibility for bentos created prior to version 1.0.0rc1
        if "bentoml_version" in yaml_content:
            del yaml_content["bentoml_version"]
        if "signatures" not in yaml_content:
            yaml_content["signatures"] = {}
        if "context" in yaml_content and "pip_dependencies" in yaml_content["context"]:
            del yaml_content["context"]["pip_dependencies"]
            yaml_content["context"]["framework_versions"] = {}

        try:
            model_info = bentoml_cattr.structure(yaml_content, ModelInfo)
        except TypeError as e:  # pragma: no cover - simple error handling
            raise BentoMLException(f"unexpected field in {MODEL_YAML_FILENAME}: {e}")
        return model_info

    def validate(self):
        # Validate model.yml file schema, content, bentoml version, etc
        # add tests when implemented
        ...


# Remove after attrs support ForwardRef natively
attr.resolve_types(ModelInfo, globals(), locals())

bentoml_cattr.register_unstructure_hook_func(
    lambda cls: issubclass(cls, ModelInfo),
    # Ignore tag, tag is saved via the name and version field
    make_dict_unstructure_fn(ModelInfo, bentoml_cattr, tag=override(omit=True)),  # type: ignore (incomplete types)
)


def copy_model(
    model_tag: t.Union[Tag, str],
    *,
    src_model_store: ModelStore,
    target_model_store: ModelStore,
):
    """copy a model from src model store to target modelstore, and do nothing if the
    model tag already exist in target model store
    """
    try:
        target_model_store.get(model_tag)  # if model tag already found in target
        return
    except NotFound:
        pass

    model = src_model_store.get(model_tag)
    model.save(target_model_store)


def _ModelInfo_dumper(dumper: yaml.Dumper, info: ModelInfo) -> yaml.Node:
    return dumper.represent_dict(info.to_dict())


yaml.add_representer(ModelInfo, _ModelInfo_dumper)  # type: ignore (incomplete yaml types)
