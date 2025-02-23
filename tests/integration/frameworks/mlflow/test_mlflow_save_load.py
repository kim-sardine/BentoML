import os
from pathlib import Path

import numpy as np
import psutil
import pytest
import mlflow.sklearn

import bentoml
from bentoml.exceptions import BentoMLException
from tests.utils.helpers import assert_have_file_extension
from tests.utils.frameworks.sklearn_utils import sklearn_model_data

current_file = Path(__file__).parent

MODEL_NAME = __name__.split(".")[-1]

# fmt: off
res_arr = np.array(
    [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
     0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
     0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
     0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
     1, 1, 1, 1, 1, 1, 2, 1, 2, 1, 1, 1, 1, 1, 1, 1,
     1, 1, 1, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
     1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2,
     2, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 2, 2,
     2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2,
     2, 2, 2, 2, 2, 2]
)
# fmt: on


def test_mlflow_save():
    with pytest.raises(BentoMLException):
        bentoml.mlflow.save()


def test_mlflow_save_load():
    (model, data) = sklearn_model_data()
    uri = Path(current_file, "sklearn_clf")
    if not uri.exists():
        mlflow.sklearn.save_model(model, uri.resolve())
    tag = bentoml.mlflow.import_from_uri(MODEL_NAME, str(uri.resolve()))
    model_info = bentoml.models.get(tag)
    assert_have_file_extension(os.path.join(model_info.path, "sklearn_clf"), ".pkl")

    loaded = bentoml.mlflow.load(tag)
    np.testing.assert_array_equal(loaded.predict(data), res_arr)  # noqa


@pytest.fixture()
def invalid_save_with_no_mlmodel():
    uri = Path(current_file, "sklearn_clf").resolve()
    tag = bentoml.mlflow.import_from_uri("sklearn_clf", str(uri))
    info = bentoml.models.get(tag)
    os.remove(str(Path(info.path, "sklearn_clf", "MLmodel").resolve()))
    return tag


def test_invalid_load(invalid_save_with_no_mlmodel):
    with pytest.raises(FileNotFoundError):
        _ = bentoml.mlflow.load(invalid_save_with_no_mlmodel)


def test_mlflow_load_runner():
    (_, data) = sklearn_model_data()
    uri = Path(current_file, "sklearn_clf").resolve()
    tag = bentoml.mlflow.import_from_uri(MODEL_NAME, str(uri))
    runner = bentoml.mlflow.load_runner(tag)
    from bentoml._internal.frameworks.mlflow import _PyFuncRunner

    assert isinstance(runner, _PyFuncRunner)

    assert tag in runner.required_models
    assert runner.num_replica == 1

    res = runner.run_batch(data)
    assert all(res == res_arr)


@pytest.mark.parametrize(
    "uri",
    [
        Path(current_file, "SimpleMNIST").resolve(),
        Path(current_file, "NestedMNIST").resolve(),
    ],
)
def test_mlflow_invalid_import_mlproject(uri):
    with pytest.raises(BentoMLException):
        _ = bentoml.mlflow.import_from_uri(MODEL_NAME, str(uri))
