# QuaPy Lab

## Goal

Implement a visual, web-based laboratory for designing, running, comparing, and documenting quantification experiments based on the [QuaPy](https://github.com/HLT-ISTI/QuaPy) package.

QuaPy Lab covers the complete experimental lifecycle, from importing data to exporting a reproducible fitted model. It may also apply a trained model interactively to an uploaded unlabelled dataset for validation or exploratory use. Continuous ingestion, externally triggered jobs, and production inference are not responsibilities of QuaPy Lab.

When deployed as a web application, QuaPy Lab is multi-user: users authenticate, organize work into groups, and share the projects owned by those groups according to role-based permissions. The native desktop application remains a passwordless personal mode with one implicit local user and does not expose group-management features.

These are some hints on required abilities:
  - visual interface with boxes with fields for configuration and connectors to define data flow (similar to comfyui)
  - Management of labelled datasets for experiments and unlabelled datasets for interactive validation of trained models
  - Library of quantification algorithms and tools mapped from QuaPy
  - Setup of quantification experiments, e.g., training, validation, optimization, testing, and comparison of quantifiers on datasets
  - Detailed reporting on experiments, e.g., choice of metrics, visualization of results, optimal parameters, timings.
  - Export of fitted models together with their preprocessing pipeline, input schema, class order, dependency information, and provenance
  - In web mode, authentication and group-based project sharing with owner, administrator, editor, and viewer roles

### Product boundaries

The web interface uses an internal HTTP API for all Lab operations, including datasets, workflows, experiment runs, logs, results, artifacts, model export, and interactive prediction. This API is an implementation boundary for the application; it is not initially a public production integration contract.

Web deployments require authentication and enforce group authorization on every project resource and artifact. Desktop mode supplies an implicit local identity and private group to the same application services; it does not require login and must not accidentally enable remote access or multi-user administration.

A stable automation API for scripts or CI systems may be added later. If added, it should cover the complete experiment lifecycle rather than dataset upload alone: submit data, create or select a workflow revision, trigger and cancel a run, inspect status, and download results.

Production inference belongs to a separate future component, **QuaPy Model Server**. It loads an approved model bundle exported by QuaPy Lab, validates incoming batches against the bundle's input schema, and returns prevalence estimates. It must be independently deployable and must not contain the Lab's workflow editor, experiment runner, or dataset library.

### Requirements

The application is implemented in Python.
It may run standalone, starting its own localhost server, and it can also be run in a WSGI server.
It can also be packaged as a single-file executable that runs in a native window, hiding its web application nature.
Windows and Linux are required supported platforms. Platform-specific executables are built and tested separately on each operating system; Windows is the initial development target, not the only release target.

### Steps

* Write a high-level list of features of the applications and subgoals. 

