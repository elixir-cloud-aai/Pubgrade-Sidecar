from setuptools import setup, find_packages
from app import __version__
#
# with open("README.md", "r") as fh:
#     long_description = fh.read()

setup(
    name="pubgrade sidecar",
    version=__version__,
    author="ELIXIR Cloud & AAI",
    author_email="akash2237778@gmail.com",
    description=(
        "Pubgrade sidecar is a microservice to update deployment on a k8s-cluster."
    ),
    # long_description=long_description,
    long_description_content_type="text/markdown",
    license="Apache License 2.0",
    url="https://github.com/elixir-cloud-aai/Pubgrade.git",
    packages=find_packages(),
    keywords=(
        "api pubgrade_sidecar ci-cd devops docker elixir flask ga4gh openapi "
        "pubgrade pubsub python server subscription swagger upgrade "
    ),
    classifiers=[
        "License :: OSI Approved :: Apache Software License",
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Information Technology",
        "Topic :: Scientific/Engineering :: Bio-Informatics",
        "Natural Language :: English",
        "Programming Language :: Python :: 3.6",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
    ],
    install_requires=[],
)
