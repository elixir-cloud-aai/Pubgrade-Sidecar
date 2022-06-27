from kubernetes import client, config
import os
import re


from flask import current_app, request
from werkzeug.exceptions import InternalServerError, NotFound, Unauthorized



namespace = os.getenv("NAMESPACE", "pubgrade_sidecar")

if os.getenv("KUBERNETES_SERVICE_HOST"):
    config.load_incluster_config()
else:
    config.load_kube_config()

v1 = client.CoreV1Api()
apiV1 = client.AppsV1Api()


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

    image = request.json["image_name"]
    image_tag = request.json["tag"]
    old_image = getImage(deployment_name)
    image = image + ":" + image_tag
    if re.split(":", old_image)[0] != re.split(":", image)[0]:
        return "Cannot change image, only tag"

    patch = [
        {
            "op": "replace",
            "value": image,
            "path": "/spec/template/spec/containers/0/image",
        }
    ]
    response = apiV1.patch_namespaced_deployment(
        name=deployment_name, namespace=namespace, body=patch
    )

    return str(response.spec.template.spec.containers[0].image)
