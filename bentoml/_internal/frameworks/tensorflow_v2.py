from __future__ import annotations

import os
import pickle
import typing as t
import logging
import functools
import itertools
import contextlib
from typing import TYPE_CHECKING

import bentoml
from bentoml import Tag
from bentoml import Runnable
from bentoml.models import ModelContext
from bentoml.exceptions import NotFound
from bentoml.exceptions import MissingDependencyException

from ..types import LazyType
from ..models.model import ModelSignature
from ..runner.utils import Params
from ..runner.container import Payload
from ..runner.container import DataContainer
from ..runner.container import DataContainerRegistry
from ..utils.tensorflow import get_tf_version
from ..utils.tensorflow import hook_loaded_model

logger = logging.getLogger(__name__)

try:
    import tensorflow as tf  # type: ignore
except ImportError:  # pragma: no cover
    raise MissingDependencyException(
        """\
    `tensorflow` is required in order to use `bentoml.tensorflow`.
    Instruction: `pip install tensorflow`
    """
    )


if TYPE_CHECKING:
    from .. import external_typing as ext
    from ..models.model import ModelSignatureDict
    from ..external_typing import tensorflow as tf_ext

    TFArgType = t.Union[t.List[t.Union[int, float]], ext.NpNDArray, tf_ext.Tensor]

MODULE_NAME = "bentoml.tensorflow"
API_VERSION = "v1"


def get(tag_like: str | Tag) -> bentoml.Model:
    model = bentoml.models.get(tag_like)
    if model.info.module not in (MODULE_NAME, __name__):
        raise NotFound(
            f"Model {model.tag} was saved with module {model.info.module}, failed loading with {MODULE_NAME}."
        )
    return model


def load_model(
    bento_model: str | Tag | bentoml.Model,
    device_name: str = "/device:CPU:0",
) -> "tf_ext.AutoTrackable" | "tf_ext.Module":
    """
    Load a tensorflow model from BentoML local modelstore with given name.

    Args:
        bento_model (``str`` ``|`` :obj:`~bentoml.Tag` ``|`` :obj:`~bentoml.Model`):
            Either the tag of the model to get from the store, or a BentoML `~bentoml.Model`
            instance to load the model from.
        device_name (``str`` | ``None``):
            The device id to load the model on. The device id format should be compatible with `tf.device <https://www.tensorflow.org/api_docs/python/tf/device>`_


    Returns:
        :obj:`SavedModel`: an instance of :obj:`SavedModel` format from BentoML modelstore.

    Examples:

    .. code-block:: python

        import bentoml

        # load a model back into memory
        model = bentoml.tensorflow.load_model("my_tensorflow_model")

    """  # noqa: LN001
    if not isinstance(bento_model, bentoml.Model):
        bento_model = get(bento_model)

    if "GPU" in device_name:
        physical_devices = tf.config.list_physical_devices("GPU")
        tf.config.experimental.set_memory_growth(physical_devices[0], True)

    with tf.device(device_name):
        tf_model: "tf_ext.AutoTrackable" = tf.saved_model.load(bento_model.path)  # type: ignore
        return hook_loaded_model(tf_model, MODULE_NAME)


def save_model(
    name: str,
    model: t.Union["tf_ext.KerasModel", "tf_ext.Module"],
    *,
    tf_signatures: "tf_ext.ConcreteFunction" | None = None,
    tf_save_options: "tf_ext.SaveOptions" | None = None,
    signatures: t.Dict[str, ModelSignature]
    | t.Dict[str, ModelSignatureDict]
    | None = None,
    labels: t.Dict[str, str] | None = None,
    custom_objects: t.Dict[str, t.Any] | None = None,
    metadata: t.Dict[str, t.Any] | None = None,
) -> Tag:

    """
    Save a model instance to BentoML modelstore.

    Args:
        name (:code:`str`):
            Name for given model instance. This should pass Python identifier check.
        model (``keras.Model`` | ``tf.Module``):
            Instance of model to be saved
        tf_signatures (:code:`Union[Callable[..., Any], dict]`, `optional`, default to :code:`None`):
            Refers to `Signatures explanation <https://www.tensorflow.org/api_docs/python/tf/saved_model/save>`_
            from Tensorflow documentation for more information.
        tf_save_options (`tf.saved_model.SaveOptions`, `optional`, default to :code:`None`):
            :obj:`tf.saved_model.SaveOptions` object that specifies options for saving.
        signatures (:code: `Dict[str, bool | BatchDimType | AnyType | tuple[AnyType]]`)
            Methods to expose for running inference on the target model. Signatures are
             used for creating Runner instances when serving model with bentoml.Service
        labels (:code:`Dict[str, str]`, `optional`, default to :code:`None`):
            user-defined labels for managing models, e.g. team=nlp, stage=dev
        custom_objects (:code:`Dict[str, Any]]`, `optional`, default to :code:`None`):
            user-defined additional python objects to be saved alongside the model,
            e.g. a tokenizer instance, preprocessor function, model configuration json
        metadata (:code:`Dict[str, Any]`, `optional`,  default to :code:`None`):
            Custom metadata for given model.

    Raises:
        ValueError: If :obj:`obj` is not trackable.

    Returns:
        :obj:`~bentoml.Tag`: A :obj:`tag` with a format `name:version` where `name` is
        the user-defined model's name, and a generated `version` by BentoML.

    Examples:

    .. code-block:: python

        import tensorflow as tf
        import numpy as np
        import bentoml

        class NativeModel(tf.Module):
            def __init__(self):
                super().__init__()
                self.weights = np.asfarray([[1.0], [1.0], [1.0], [1.0], [1.0]])
                self.dense = lambda inputs: tf.matmul(inputs, self.weights)

            @tf.function(
                input_signature=[tf.TensorSpec(shape=[1, 5], dtype=tf.float64, name="inputs")]
            )
            def __call__(self, inputs):
                return self.dense(inputs)

        # then save the given model to BentoML modelstore:
        model = NativeModel()
        tag = bentoml.tensorflow.save_model("native_toy", model)

    .. note::

       :code:`bentoml.tensorflow.save_model` API also support saving `RaggedTensor <https://www.tensorflow.org/guide/ragged_tensor>`_ model and Keras model. If you choose to save a Keras model
       with :code:`bentoml.tensorflow.save_model`, then the model will be saved under a :obj:`SavedModel` format instead of :obj:`.h5`.

    """  # noqa
    context = ModelContext(
        framework_name="tensorflow",
        framework_versions={"tensorflow": get_tf_version()},
    )

    # will add signatures inference from tf_signatures later
    if signatures is None:
        signatures = {
            "__call__": {
                "batchable": False,
            }
        }

        logger.info(
            f"Using the default model signature {signatures} for TensorFlow models."
        )

    with bentoml.models.create(
        name,
        module=MODULE_NAME,
        api_version=API_VERSION,
        options=None,
        context=context,
        labels=labels,
        custom_objects=custom_objects,
        metadata=metadata,
        signatures=signatures,  # type: ignore
    ) as bento_model:

        tf.saved_model.save(
            model,
            bento_model.path,
            signatures=tf_signatures,
            options=tf_save_options,
        )

        return bento_model.tag


def get_runnable(
    bento_model: bentoml.Model,
):
    """
    Private API: use :obj:`~bentoml.Model.to_runnable` instead.
    """

    partial_kwargs: t.Dict[str, t.Any] = bento_model.info.options.get(
        "partial_kwargs", dict()
    )

    class TensorflowRunnable(Runnable):
        SUPPORT_NVIDIA_GPU = True
        SUPPORT_CPU_MULTI_THREADING = True

        def __init__(self):
            super().__init__()
            if len(tf.config.list_physical_devices("GPU")) > 0:
                # In Multi-GPU scenarios, the visible cuda devices will be set for each Runner worker
                # by the runner's Scheduling Strategy. So that the Runnable implementation only needs
                # to find the first GPU device visible to current process.
                self.device_name = "/device:GPU:0"
            else:
                self.device_name = "/device:CPU:0"

            self.model = load_model(bento_model, device_name=self.device_name)
            self.methods_cache: t.Dict[str, t.Callable[..., t.Any]] = {}
            self.session_stack = contextlib.ExitStack()
            self.session_stack.enter_context(tf.device(self.device_name))

        def __del__(self):
            try:
                self.session_stack.close()
            except RuntimeError:
                pass

    def _gen_run_method(runnable_self: TensorflowRunnable, method_name: str):
        raw_method = getattr(runnable_self.model, method_name)
        method_partial_kwargs = partial_kwargs.get(method_name)
        if method_partial_kwargs:
            raw_method = functools.partial(raw_method, **method_partial_kwargs)

        def _mapping(item: "TFArgType") -> "tf_ext.TensorLike":
            if not LazyType["tf_ext.TensorLike"]("tensorflow.Tensor").isinstance(item):
                return t.cast("tf_ext.TensorLike", tf.convert_to_tensor(item))
            else:
                return item

        def _run_method(
            runnable_self: TensorflowRunnable, *args: "TFArgType", **kwargs: "TFArgType"
        ) -> "ext.NpNDArray":
            params = Params["TFArgType"](*args, **kwargs)
            params = params.map(_mapping)
            res = raw_method(*params.args, **params.kwargs)
            return t.cast("ext.NpNDArray", res.numpy())

        return _run_method

    def add_run_method(method_name: str, options: ModelSignature):
        def run_method(
            runnable_self: TensorflowRunnable, *args: "TFArgType", **kwargs: "TFArgType"
        ) -> "ext.NpNDArray":
            _run_method = runnable_self.methods_cache.get(
                method_name
            )  # is methods_cache nessesary?
            if not _run_method:
                _run_method = _gen_run_method(runnable_self, method_name)
                runnable_self.methods_cache[method_name] = _run_method

            return _run_method(runnable_self, *args, **kwargs)

        TensorflowRunnable.add_method(
            run_method,
            name=method_name,
            batchable=options.batchable,
            batch_dim=options.batch_dim,
            input_spec=options.input_spec,
            output_spec=options.output_spec,
        )

    for method_name, options in bento_model.info.signatures.items():
        add_run_method(method_name, options)

    return TensorflowRunnable


class TensorflowTensorContainer(
    DataContainer["tf_ext.EagerTensor", "tf_ext.EagerTensor"]
):
    @classmethod
    def batches_to_batch(
        cls, batches: t.Sequence["tf_ext.EagerTensor"], batch_dim: int = 0
    ) -> t.Tuple["tf_ext.EagerTensor", list[int]]:
        batch: "tf_ext.EagerTensor" = tf.concat(batches, axis=batch_dim)
        # TODO: fix typing mismatch @larme
        indices: list[int] = list(
            itertools.accumulate(subbatch.shape[batch_dim] for subbatch in batches)
        )  # type: ignore
        indices = [0] + indices
        return batch, indices

    @classmethod
    def batch_to_batches(
        cls, batch: "tf_ext.EagerTensor", indices: t.Sequence[int], batch_dim: int = 0
    ) -> t.List["tf_ext.EagerTensor"]:
        size_splits = [indices[i + 1] - indices[i] for i in range(len(indices) - 1)]
        return tf.split(batch, size_splits, axis=batch_dim)  # type: ignore

    @classmethod
    def to_payload(
        cls,
        batch: "tf_ext.EagerTensor",
        batch_dim: int = 0,
    ) -> Payload:

        return cls.create_payload(
            pickle.dumps(batch),
            batch_size=batch.shape[batch_dim],
        )

    @classmethod
    def from_payload(
        cls,
        payload: Payload,
    ) -> "tf_ext.EagerTensor":

        return pickle.loads(payload.data)

    @classmethod
    def batch_to_payloads(
        cls,
        batch: "tf_ext.EagerTensor",
        indices: t.Sequence[int],
        batch_dim: int = 0,
    ) -> t.List[Payload]:

        batches = cls.batch_to_batches(batch, indices, batch_dim)

        payloads = [cls.to_payload(subbatch) for subbatch in batches]
        return payloads

    @classmethod
    def from_batch_payloads(
        cls,
        payloads: t.Sequence[Payload],
        batch_dim: int = 0,
    ) -> t.Tuple["tf_ext.EagerTensor", t.List[int]]:
        batches = [cls.from_payload(payload) for payload in payloads]
        return cls.batches_to_batch(batches, batch_dim)


DataContainerRegistry.register_container(
    LazyType("tensorflow.python.framework.ops", "_EagerTensorBase"),
    LazyType("tensorflow.python.framework.ops", "_EagerTensorBase"),
    TensorflowTensorContainer,
)
