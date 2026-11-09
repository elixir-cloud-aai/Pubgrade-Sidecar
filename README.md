<!-- markdownlint-disable MD033 MD041 -- logo first; <picture> for dark mode -->
<a href="https://elixir-on.cloud/">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/images/logo_elixir_on_cloud_night.png">
    <img src="docs/images/logo_elixir_on_cloud.png" alt="ELIXIR On-Cloud">
  </picture>
</a>
<!-- markdownlint-enable MD033 MD041 -->

# Pubgrade-Sidecar

[![License][badge-license]][badge-url-license]
[![Build_status][badge-build-status]][badge-url-build-status]
[![Docs][badge-docs]][badge-url-docs]
[![Coverage][badge-coverage]][badge-url-coverage]
[![Docker_image][badge-docker]][badge-url-docker]
[![DOI][badge-doi]][badge-url-doi]

Pubgrade-Sidecar rolls out new releases of a service to its Kubernetes
Deployments when [Pubgrade](https://github.com/elixir-cloud-aai/Pubgrade)
notifies it, brought to you by [ELIXIR On-Cloud](https://elixir-on.cloud/). It
runs next to the service, in the same namespace, and changes only the image
tags of the Deployments it is allowed to update; it reports the state of each
delivery. Install it with its Helm chart in [`deployment/`][deployment], on
Kubernetes or OpenShift.

> [!NOTE]
> Pubgrade-Sidecar is still under development.

**Read the [documentation][docs]** (the Sidecar section of Pubgrade's
documentation) for the deployment, the API, the image and development.

## Citation

If you use Pubgrade-Sidecar, please cite it with its DOI
[10.5281/zenodo.24087986](https://doi.org/10.5281/zenodo.24087986), which
always resolves to the latest version on [Zenodo](https://zenodo.org/); the
Zenodo record lists the DOI of each version, too. The citation metadata is in
[`CITATION.cff`](CITATION.cff): on GitHub, "Cite this repository" in the
sidebar of the repository formats it as APA or BibTeX.

## Contributing

Contributions are welcome! See [Development][docs-development] in the
documentation and the organisation's [contributing guide][contributing], and
mind our [code of conduct][code-of-conduct].

## License

[Apache License 2.0](LICENSE)

[badge-build-status]: <https://github.com/elixir-cloud-aai/Pubgrade-Sidecar/actions/workflows/ci.yml/badge.svg?branch=dev>
[badge-coverage]: <https://codecov.io/gh/elixir-cloud-aai/Pubgrade-Sidecar/branch/dev/graph/badge.svg?branch=dev>
[badge-docker]: <https://img.shields.io/docker/v/elixircloud/pubgrade-sidecar?sort=semver&label=image&color=C39BD3>
[badge-doi]: <https://zenodo.org/badge/DOI/10.5281/zenodo.24087986.svg>
[badge-docs]: <https://img.shields.io/readthedocs/pubgrade>
[badge-license]: <https://img.shields.io/badge/license-Apache%202.0-blue.svg>
[badge-url-build-status]: <https://github.com/elixir-cloud-aai/Pubgrade-Sidecar/actions/workflows/ci.yml?query=branch%3Adev>
[badge-url-coverage]: <https://codecov.io/gh/elixir-cloud-aai/Pubgrade-Sidecar?branch=dev>
[badge-url-docker]: <https://hub.docker.com/r/elixircloud/pubgrade-sidecar/tags>
[badge-url-doi]: <https://doi.org/10.5281/zenodo.24087986>
[badge-url-docs]: <https://pubgrade.readthedocs.io/en/latest/sidecar/>
[badge-url-license]: <https://www.apache.org/licenses/LICENSE-2.0>
[code-of-conduct]: <https://github.com/elixir-cloud-aai/.github/blob/main/CODE_OF_CONDUCT.md>
[contributing]: <https://github.com/elixir-cloud-aai/.github/blob/main/CONTRIBUTING.md>
[deployment]: deployment/
[docs]: <https://pubgrade.readthedocs.io/en/latest/sidecar/>
[docs-development]: <https://pubgrade.readthedocs.io/en/latest/sidecar/development/>
