##### BASE IMAGE #####
FROM elixircloud/foca:v0.7.0-py3.9


##### METADATA #####

LABEL software="Pubgrade"
LABEL software.description="Pubgrade is a decoupled, publish-subscribe-based continuous integration (CI) and continuous delivery (CD) microservice that allows developers to notify deploments of available updates."
LABEL software.website="https://github.com/elixir-cloud-aai/Pubgrade"
LABEL software.license="https://spdx.org/licenses/Apache-2.0"
LABEL maintainer="akash2237778@gmail.com"
LABEL maintainer.organisation="ELIXIR Cloud & AAI"

RUN groupadd -r pubgrade --gid 1005 && useradd -d /home/pubgrade -ms /bin/bash -r -g pubgrade pubgrade --uid 1005

## Copy remaining app files
COPY --chown=1005:1005 ./ /app
RUN chmod 777 /app/app/api/

## Install app
RUN cd /app \
  && python setup.py develop \
  && pip install -r requirements.txt


USER 1005

CMD ["bash", "-c", "cd /app/app; python app.py"]
