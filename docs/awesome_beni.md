# Awesome Beni

A curated community list of official open-source projects, 3D-printable accessories, research, and discussion hubs for the **Mondo Robotics Beni** (`LZ110` / `LZ100`) robot. PRs welcome!

---

## Official Mondo Robotics Open Source
- **[Mondo-Robotics/open-beni](https://github.com/Mondo-Robotics/open-beni)** *(MIT)* — Official Beni URDF/MJCF robot models, 3D visual meshes (`assets/beni_base/meshes/*.obj`), RL locomotion/recovery/jump training pipeline (`mjlab` + MuJoCo Warp), exported `.pt`/`.onnx`/`.mnn` policies, and a MuJoCo WebAssembly browser racing sim.
- **[OpenBeni Playable Browser Sim (Hugging Face Spaces)](https://huggingface.co/spaces/mondo-robotics/open-beni)** — Live web deployment of `open-beni` running MuJoCo WASM + ONNX policies in the browser.

## Community & Discussion
- **[r/MondoRobotics (Reddit)](https://old.reddit.com/r/MondoRobotics/)** — Community subreddit for Beni owners, tips, mods, and discussion.

## 3D Printing & Physical Accessories
- **[MakerWorld — Beni Parts Collection](https://makerworld.com/en/collections/37985353-beni-parts)** — Community-designed 3D-printable parts, head/lens bumpers, mounts, and accessories for Beni.

## Community Research & Tooling
- **[open_ineb](https://github.com/k5njm/open_ineb)** — Reverse-engineering documentation (hardware, firmware, BLE, UCP + Zenoh-over-UCP, 434 `.proto` schemas), firmware OTA archiver (`tools/fetch_ota.py`), and standalone custom `mondo.img` TUI builder/uploader (`tools/customize_fw.py`).
