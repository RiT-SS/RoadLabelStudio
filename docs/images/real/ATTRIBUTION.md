# Real road image attribution

## Crack example

`cracks-open.jpg` comes from **Cracks and Potholes in Road Images, version 4**, by Bianka T. Passos, Mateus J. Cassaniga, Anita M. R. Fernandes, Kátya B. Medeiros and Eros Comunello. Original road imagery was supplied by DNIT.

- Dataset and license: <https://data.mendeley.com/datasets/t576ydh9v8/4>, **CC BY 4.0**, <https://creativecommons.org/licenses/by/4.0/>.
- Local distribution: the `cracks-and-potholes-in-road` component of [Pothole Mix v2](https://data.mendeley.com/datasets/kfth5g2xk3/2), assembled by Andrea Ranieri, Elia Moscoso Thompson and Silvia Biasotti. Its readme retains each component dataset’s original license.
- Source image: `918387_RS_290_290RS294095_25385.jpg`, 1024 × 640 pixels. Crop: `(x=360, y=280, width=560, height=320)`. JPEG encoding and metadata removal only; no contrast enhancement or generative changes.
- The distributed dataset includes a crack segmentation mask. This still-image example has no project model score and is not a RoadSens video frame.

## RoadSens video examples

**RoadSens-4M Dataset, version 3** — Amith Khandakar; David G. Michelson; Shaikh Golam Rabbani; Fariya Bintay Shafi; Md. Faysal Ahamed; Khondokar Radwanur Rahman; Md Abidur Rahman; Md. Fahmidun Nabi; Mohamed Arselene Ayari; Khaled Khan; Ponnuthurai Nagaratnam Suganthan.

- Dataset: <https://figshare.com/articles/dataset/RoadSens-4M_Dataset/30341143/3>
- DOI: <https://doi.org/10.6084/m9.figshare.30341143.v3>
- Dataset license: **Creative Commons Attribution 4.0 International (CC BY 4.0)**, <https://creativecommons.org/licenses/by/4.0/>

The images below are frames from the camera videos linked in the dataset's raw-data description. Attribution does not imply endorsement by the dataset authors. This notice covers the third-party road imagery, including its appearance within the workbench screenshot; it does not assign a license to the platform source code or unrelated assets.

## Files and transformations

| File | Source session | Native frame (zero-based) | Time (seconds) | Final road crop (x, y, width, height) |
| --- | --- | ---: | ---: | --- |
| `breakage.jpg` | S060 | 215 | 7.164889 | 470, 280, 960, 410 |
| `water.jpg` | S034 | 91 | 3.032644 | 740, 260, 760, 300 |
| `normal.jpg` | S059 | 163 | 5.432022 | 330, 400, 1000, 320 |

Processing: select the native frame; rotate 90 degrees clockwise; apply the existing upright road ROI `(x=0, y=1920, width=2160, height=1228)`; scale to `1536 × 874`; apply the final road-only crop listed above; encode as JPEG with source metadata removed. Crop coordinates refer to the 1536 × 874 review view. No generative edits, object replacements or perspective rectification were applied. The published crops have been visually checked for identifiable faces and license plates.

`workbench.png` is an actual RoadLabel Studio UI capture using an isolated copy of S060's v4 observations. Its displayed image is `breakage.jpg`; the caption identifies the crop and draft annotation provenance. The native frame number and timestamp are retained. No new model inference or human confirmation was performed for the capture.

## Annotation provenance

For the three RoadSens examples, the accompanying scores and notes are this project's existing **v4 direct-observation drafts**, not the original dataset authors' frame-level labels. They remain pending human review. Scores are independent values in `[0, 1]`, not statistically calibrated probabilities. They describe the original v4 review view, not a new inference on the published crop.

See [real-examples.json](../../real-examples.json) for all nine scores, original notes, crop coordinates and image checksums, and [the annotation walkthrough](../../real-examples.md) for review guidance.
