from unittest.mock import patch

import pytest
from flask import Flask
from foca.models.config import Config
from munch import DefaultMunch

from app.pubgrade_sidecar.controllers import getDeployment, getImage, \
    deleteDeployment, updateDeployment, update_image
from app.pubgrade_sidecar.errors.exceptions import Unauthorized, NotFound, DockerImageUnavailable

ENDPOINT_CONFIG = {
    "secrets":
        {"access_token": "Access@123"}

}


class MockClass:
    pass


def mock_list_namespaced_deployment(self, namespace: str):
    resp = {
        'pods': [{'metadata': {'name': "build-complete-updater"}},
                 {'metadata': {'name': "mongodb"}},
                 {'metadata': {'name': "pubgrade"}},
                 ]
    }
    pods = MockClass()
    pods.items = DefaultMunch.fromDict(resp).pods
    print(pods.items)
    return pods


def mock_read_namespaced_deployment(self, deployment_name, namespace):
    resp = {
        'spec': {
            'template': {
                'spec': {
                    'containers': [
                        {'image': "akash7778/pubgrade:latest"}
                    ]
                }
            }
        }
    }
    return DefaultMunch.fromDict(resp)


def mock_read_namespaced_deployment_not_found(self, deployment_name, namespace):
    resp = {
        'spec': {
            'template': {
                'spec': {
                    'containers': [
                    ]
                }
            }
        }
    }
    return DefaultMunch.fromDict(resp)


def mock_delete_namespaced_pod(self, deployment_name, namespace):
    pass


@patch('kubernetes.client.AppsV1Api.list_namespaced_deployment', mock_list_namespaced_deployment)
def test_get_deployments():
    assert ['build-complete-updater', 'mongodb', 'pubgrade'] == getDeployment()


@patch('kubernetes.client.AppsV1Api.read_namespaced_deployment', mock_read_namespaced_deployment)
def test_get_image():
    app = Flask(__name__)
    app.config["FOCA"] = Config(environments=ENDPOINT_CONFIG)
    with app.test_request_context('url', headers={'X-Access-Token':
                                                      'Access@123'}):
        assert 'akash7778/pubgrade:latest' == getImage('pubgrade')


def test_get_image_unauthorized():
    app = Flask(__name__)
    app.config["FOCA"] = Config(environments=ENDPOINT_CONFIG)
    with app.test_request_context('url', headers={'X-Access-Token':
                                                      'wrong_access_token'}):
        with pytest.raises(Unauthorized):
            getImage('pubgrade')


@patch('kubernetes.client.AppsV1Api.read_namespaced_deployment', mock_read_namespaced_deployment_not_found)
def test_get_image_not_found():
    app = Flask(__name__)
    app.config["FOCA"] = Config(environments=ENDPOINT_CONFIG)
    with app.test_request_context('url', headers={'X-Access-Token':
                                                      'Access@123'}):
        with pytest.raises(NotFound):
            getImage('pubgrade')


@patch('kubernetes.client.CoreV1Api.delete_namespaced_pod', mock_delete_namespaced_pod)
def test_delete_deployment():
    app = Flask(__name__)
    app.config["FOCA"] = Config(environments=ENDPOINT_CONFIG)
    with app.test_request_context('url', headers={'X-Access-Token':
                                                      'Access@123'}):
        assert "deleted: pubgrade" == deleteDeployment('pubgrade')


def test_delete_deployment_unauthorized():
    app = Flask(__name__)
    app.config["FOCA"] = Config(environments=ENDPOINT_CONFIG)
    with app.test_request_context('url', headers={'X-Access-Token':
                                                      'wrong_access_token'}):
        with pytest.raises(Unauthorized):
            deleteDeployment('pubgrade')


def mock_patch_namespaced_deployment(self, name, namespace, body):
    resp = {
        'spec': {
            'template': {
                'spec': {
                    'containers': [
                        {'image': "akash7778/pubgrade:latest"}
                    ]
                }
            }
        }
    }
    return DefaultMunch.fromDict(resp)


@patch('kubernetes.client.AppsV1Api.read_namespaced_deployment', mock_read_namespaced_deployment)
@patch('kubernetes.client.AppsV1Api.patch_namespaced_deployment', mock_patch_namespaced_deployment)
@patch('app.pubgrade_sidecar.controllers.check_docker_image_availability', return_value=True)
def test_update_deployment(mock_controllers):
    app = Flask(__name__)
    app.config["FOCA"] = Config(environments=ENDPOINT_CONFIG)
    with app.test_request_context('url', headers={
        'X-Access-Token': 'Access@123'}, json={'image_name':
                                                   'akash7778/pubgrade', 'tag': '1.0'}):
        response = updateDeployment('pubgrade')
        assert 'Updating image in progress...' == response.json["message"]


@patch("app.pubgrade_sidecar.controllers.time.sleep")
@patch("app.pubgrade_sidecar.controllers.check_docker_image_availability")
@patch('kubernetes.client.AppsV1Api.patch_namespaced_deployment')
def test_update_image_successful(mock_patch_deployment, mock_check_image, mock_sleep):
    mock_check_image.return_value = True

    update_image("new_repo/image", "new_tag", "test_deployment", "test_patch", 0)

    mock_patch_deployment.assert_called_once()


@patch("app.pubgrade_sidecar.controllers.time.sleep")
@patch("app.pubgrade_sidecar.controllers.check_docker_image_availability")
@patch('kubernetes.client.AppsV1Api.patch_namespaced_deployment')
def test_update_image_retry_exhausted(mock_patch_deployment, mock_check_image, mock_sleep):
    mock_check_image.return_value = False

    with pytest.raises(DockerImageUnavailable):
        update_image("new_repo/image", "new_tag", "test_deployment", "test_patch", 3)

    assert (mock_patch_deployment.call_count, 0)
