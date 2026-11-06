# Pubgrade-Sidecar

Pubgrade-Sidecar rolls out new releases of a service to its Kubernetes
Deployments when [Pubgrade](https://github.com/elixir-cloud-aai/Pubgrade)
notifies it. It runs next to the service, in the same namespace, and changes
only the image tags of the Deployments it is allowed to update; it reports the
state of each delivery. Install it with its Helm chart, on Kubernetes or
OpenShift.

!!! note

    Pubgrade-Sidecar is still under development.

## Contents

- [Deployment](deployment.md): install the sidecar with its Helm chart, on
  Kubernetes and OpenShift, and upgrade from chart version 0.1.
- [API](api.md): the calls, their status codes and the status report of a
  delivery.
- [Development](development.md): the development setup, the checks and the
  documentation.
- [Code reference](reference.md): the controllers and errors, generated from
  their docstrings.

## Citation

If you use Pubgrade-Sidecar, please cite it with the metadata in
[`CITATION.cff`](https://github.com/elixir-cloud-aai/Pubgrade-Sidecar/blob/dev/CITATION.cff):
on GitHub, "Cite this repository" in the sidebar of the repository formats it
as APA or BibTeX.

## License

The sidecar is released under the
[Apache License 2.0](https://github.com/elixir-cloud-aai/Pubgrade-Sidecar/blob/dev/LICENSE).
