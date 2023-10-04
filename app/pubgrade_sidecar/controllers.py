from kubernetes import client, config
import os
import re

from flask import current_app, request, jsonify
import requests
import time
from concurrent.futures import ThreadPoolExecutor
from app.pubgrade_sidecar.errors.exceptions import Unauthorized, NotFound, DockerImageUnavailable

namespace = os.getenv("NAMESPACE", "pubgrade_sidecar")

if os.getenv("KUBERNETES_SERVICE_HOST"):
    config.load_incluster_config()
else:
    config.load_kube_config()

v1 = client.CoreV1Api()
apiV1 = client.AppsV1Api()
MAX_RETRY = 3
RETRY_INTERVAL = 30




def getDeployment():
    pod_list = apiV1.list_namespaced_deployment(namespace)
    pods = []
    for pod in pod_list.items:
        pods.append(pod.metadata.name)
    return pods


def getImage(deployment_name: str):
    access_token = current_app.config["FOCA"].environments["secrets"][
        "access_token"
    ]
    x_access_token = request.headers["X-Access-Token"]

    if access_token != x_access_token:
        raise Unauthorized

    try:
        deployment = apiV1.read_namespaced_deployment(
            deployment_name, namespace
        )
        return deployment.spec.template.spec.containers[0].image
    except Exception:
        raise NotFound


def deleteDeployment(deployment_name: str):
    access_token = current_app.config["FOCA"].environments["secrets"][
        "access_token"
    ]
    x_access_token = request.headers["X-Access-Token"]

    if access_token != x_access_token:
        raise Unauthorized

    try:
        v1.delete_namespaced_pod(
            deployment_name, namespace
        )
    except Exception:
        raise NotFound
    return "deleted: " + deployment_name


def updateDeployment(deployment_name: str):
    access_token = current_app.config["FOCA"].environments["secrets"][
        "access_token"
    ]
    x_access_token = request.headers["X-Access-Token"]

    if access_token != x_access_token:
        raise Unauthorized

    image_repo = request.json["image_name"]
    image_tag = request.json["tag"]
    old_image = getImage(deployment_name)
    image = image_repo + ":" + image_tag
    if re.split(":", old_image)[0] != re.split(":", image)[0]:
        return "Cannot change image, only tag"

    patch = [
        {
            "op": "replace",
            "value": image,
            "path": "/spec/template/spec/containers/0/image",
        }
    ]

    # TODO: Re write retry logic with kafka pub-sub queue, separating retry from server process into a different pod
    with ThreadPoolExecutor() as executor:
        executor.submit(
            update_image, image_repo, image_tag, deployment_name, patch, 0
        )
        return jsonify({"message": "Updating image in progress..."})


def update_image(image_repo, image_tag, deployment_name, patch, retry):
    if retry > MAX_RETRY:
        raise DockerImageUnavailable
    if check_docker_image_availability(image_repo, image_tag):
        return apiV1.patch_namespaced_deployment(
            name=deployment_name, namespace=namespace, body=patch
        )
    else:
        time.sleep(RETRY_INTERVAL)
        update_image(image_repo, image_tag, deployment_name, patch, retry + 1)


def check_docker_image_availability(image_name, tag='latest'):
    base_url = 'https://hub.docker.com'
    url = f'{base_url}/v2/repositories/{image_name}/tags/{tag}/'
    response = requests.get(url)
    return response.status_code == 200
