# Sources — Hydris documentation (consulted on 03/11/2026)

## Website and download
- Project Q website: https://www.project-q.ai/
- Integrations page: https://www.project-q.ai/integrations
- Repository: https://github.com/projectqai/hydris
- Windows release v1.0.0: https://github.com/projectqai/hydris/releases/download/v1.0.0/hydris-desktop-windows-amd64-v1.0.0.zip
- Latest releases (all platforms): https://github.com/projectqai/hydris/releases/latest
- License: Apache 2.0

## Documentation (https://projectqai.github.io/docs)

| Page | Link | Why it matters to us |
|---|---|---|
| Docs home | https://projectqai.github.io/docs | installation, three guides: Operator / Integration / Developer |
| Integration Guide | https://projectqai.github.io/docs/integration | list of already supported protocols (ATAK, ASTERIX, MAVLink...) and gRPC API |
| **gRPC / HTTP API** | https://projectqai.github.io/docs/integration/grpc | **Python** quickstart: `uv add grpcio protobuf` + `pip install git+https://github.com/projectqai/proto.git#subdirectory=python`; `WorldServiceStub` client on `localhost:50051`, `Push(EntityChangeRequest)` method |
| **External Detectors** | https://projectqai.github.io/docs/integration/external-detectors | how to push detections from an external pipeline (CV, radar, **acoustic sensors**): deterministic IDs, `controller`, `lifetime`, `confidence`, `evidence`, `ExpireEntity` |
| **Entity Cookbook** | https://projectqai.github.io/docs/developer/cookbook | YAML examples; **"Perimeter Sensors"** section = our case (a sensor that only knows "something is nearby"); sensors as assets (`device.state`), `metric`, `capture`, `artifact` (WAV files) |
| Acoustic sensor example | https://projectqai.github.io/examples/audio-sensor.yaml | complete YAML file of the ground acoustic sensor, to compare with our draft |
| Developer Guide | https://projectqai.github.io/docs/developer | TypeScript plugins, published as OCI images |
| Developing Plugins | https://projectqai.github.io/docs/developer/plugins | if one day we want a plugin in the catalog |
| .proto files (complete API reference) | https://github.com/projectqai/proto | exact list of fields and of the taxonomy |

## Command line tool
YAML examples are loaded with:

```
hydris ec apply file.yaml
```

Useful for trying `draft_entities.yaml` without writing code.
