# Credits

Third-party projects this repository references, derives from, or vendors. See
`LICENSE` for this project's own license (AGPL-3.0-or-later).

## Vendored assets

- **[airockchip/rknn_model_zoo](https://github.com/airockchip/rknn_model_zoo)**
  (Apache-2.0, tag `v2.3.2`) — copied from that project:
  `piport/models/anchors_yolov5.txt`,
  `piport/models/coco_80_labels_list.txt`, `rknn_convert/anchors_yolov5.txt`,
  `rknn_convert/coco_80_labels_list.txt` and `rknn_convert/coco_subset_20.txt`.
  `detector.py`'s YOLOv5 post-processing and `rknn_convert/convert.py` are adapted
  from its example. Apache-2.0 text: [`LICENSES/Apache-2.0.txt`](LICENSES/Apache-2.0.txt).

## Detection model

- **[Ultralytics YOLOv5](https://github.com/ultralytics/yolov5)** (AGPL-3.0) —
  the detection architecture. The converted `.rknn` is an AGPL-3.0 derivative of
  YOLOv5s, so redistributed model binaries carry AGPL-3.0, the same license as
  this project.

## Non-free, not distributed

- **Rockchip `rknn-toolkit2` / `rknn-toolkit-lite2`** — installed by the user
  from PyPI under Rockchip's proprietary RKNN SDK license; not covered by this
  repository's license and not redistributed here.

## Protocol research

- **[rjmotion/pyunifiwire](https://github.com/rjmotion/pyunifiwire)** — UBNT L2
  discovery field/TLV notes.
- **[danielwoz/ubiquiti-protect-onvif-event-listener](https://github.com/danielwoz/ubiquiti-protect-onvif-event-listener)**
  — event/schema reference. Its direct database-write approach is deliberately
  **not** used here; this project is protocol-only.

## Inspiration

- **[dciancu/unifi-protect-unvr-docker-arm64](https://github.com/dciancu/unifi-protect-unvr-docker-arm64)**
  — the project that got this author into UniFi tinkering, and whose methods
  helped in understanding how parts of the ecosystem work.
