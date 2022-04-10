##### BASE IMAGE #####
FROM elixircloud/foca:latest

##### METADATA ##### 

LABEL software="Pubgrade-Sidecar"
LABEL software.description="The project is to implement a microservice which would be co-deployed along with pubgrade in Kubernetes cluster at deployments to listen for update notifications from pubgrade and update the subscribed service accordingly."
LABEL software.website="https://github.com/akash2237778/Pubgrade-Sidecar"
LABEL software.license="https://spdx.org/licenses/Apache-2.0"
LABEL maintainer="akash2237778@gmail.com"
LABEL maintainer.organisation="ELIXIR Cloud & AAI"

RUN groupadd -r pubgrade --gid 1000 && useradd -d /home/pubgrade -ms /bin/bash -r -g pubgrade pubgrade --uid 1000

## Copy app files
COPY --chown=1000:1000 ./ /app

RUN cd /app \
    && pip install kubernetes \
    && pip install gitpython

USER 1000

CMD ["bash", "-c", "cd /app/app; python app.py"]