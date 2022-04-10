import logging
from kubernetes import client, config
import os
import shutil
import time
import re


from flask import current_app, request
from werkzeug.exceptions import InternalServerError, NotFound, Unauthorized
import yaml
from git import Repo
import json


namespace = os.getenv("NAMESPACE", "pubgrade-sidecar")

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

    api_instance = client.CoreV1Api()
    body = client.V1DeleteOptions()
    api_response = api_instance.delete_namespaced_pod(
        deployment_name, namespace
    )
    return "deleted: " + deployment_name + "  " + api_response


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

    print("Patch: %s" % patch)
    response = apiV1.patch_namespaced_deployment(
        name=deployment_name, namespace=namespace, body=patch
    )

    return str(response.spec.template.spec.containers[0].image)
