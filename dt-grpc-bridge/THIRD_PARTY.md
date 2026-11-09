# Third-party files used by dt-grpc-bridge

`setup.sh` downloads these files from https://github.com/drawthingsai/draw-things-community at commit
`37d071b0697b2c6965276538cfe8c05048dd87e1`, checks each one's SHA-256, and generates
`imageService_pb2.py` / `imageService_pb2_grpc.py` from the `.proto` with `grpcio-tools`.

| Local file | Upstream path |
|---|---|
| `imageService.proto` | `Libraries/GRPC/Models/Sources/imageService/imageService.proto` |
| `config.fbs` | `Libraries/DataModels/Sources/config.fbs` |
| `dt_root_ca.crt` | `Libraries/BinaryResources/Resources/root_ca.crt` |

That repository is licensed under the **GNU General Public License v3.0**
(https://github.com/drawthingsai/draw-things-community/blob/main/LICENSE.md). Because GPL-3.0 terms
differ from this project's MIT license, the files are **not** included in this repository; they are
fetched to your machine when you run `setup.sh`, and are covered by GPL-3.0 there. They are listed in
`.gitignore` so they are not committed back.

`bridge.py` itself is MIT. It contains no code copied from that repository; it does carry the
GenerationConfiguration field positions and the sampler enum order from `config.fbs`, which it needs to
build a request in Draw Things' wire format.

"Draw Things" is the name of a third-party app. This project is not affiliated with or endorsed by its
authors.
