# AgriSense BW
AgriSense BW is an AI-powered smart agriculture platform that empowers farmers with data-driven insights to improve productivity, sustainability, and livestock management. The system combines weather data, satellite imagery, IoT sensors, and machine learning to provide crop health monitoring, disease detection, yield prediction, and planting recommendations. It can also track livestock using GPS-enabled devices, monitor animal movement and health, and detect unusual behaviour that may indicate illness or theft. By analysing livestock health data and disease patterns, the platform can provide early warnings of potential disease outbreaks, helping farmers and veterinary services respond quickly to prevent the spread of infections. AgriSense BW supports improved food security, efficient resource management, and the long-term growth of Botswana's agricultural sector.

## Project layout

| Folder              | Contents                                                                                                         |
|---------------------|------------------------------------------------------------------------------------------------------------------|
| `data/input/`       | Input data: `crop_data.csv`, `livestock_data.csv`, `public_reference_yields.csv`                                 |
| `data/alerts/`      | Alerts from the crop and livestock models, and the combined `final_alerts.csv`                                   |
| `data/`             | `advice_templates.csv` (English and Setswana advice), and the generated `agrisense.db`                           |
| `models/livestock/` | Livestock monitoring notebook and chart                                                                          |
| `src/`              | Early-warning engine, database builder and the FastAPI dashboard (`main.py`, `data.py`, `templates/`, `static/`) |
| `firmware/`         | Hardware prototype: collar node (XIAO nRF52840), gateway (ESP32) and a simulator, see `firmware/README.md`        |
| `slides/`           | Slides summarizing the project                                                                                   |
| `doc/`              | Team task plan, design files and the beginner's code guide `CODE_GUIDE.md`                                                                                 |

## How to run the prototype

All commands run from this folder. Install the packages once with `pip install -r requirements.txt`.

| Step                 | Command                                                                  | Result                                                                   |
|----------------------|--------------------------------------------------------------------------|--------------------------------------------------------------------------|
| Early-warning engine | `python -m src.alert_engine`                                             | `data/alerts/final_alerts.csv` from the crop and livestock alerts        |
| Database             | `python -m src.build_database`                                           | `data/agrisense.db` (SQLite) from all CSV files; alert statuses are kept |
| Dashboard            | `uvicorn src.main:app --reload`                                          | http://127.0.0.1:8000, API documentation at `/docs`                      |
| With live sensors    | `uvicorn src.main:app --host 0.0.0.0 --reload`                           | The gateway can reach the dashboard over Wi-Fi; see the **Sensors** tab   |
| Sensor test data     | `python firmware/simulate_gateway.py`                                    | Made-up collar and rain readings, without hardware                       |
| Sensor daily export  | `python -m src.sensor_export`                                            | Daily sensor summaries in `data/input/`, shaped like `livestock_data.csv` |

All crop and livestock data in this repository is sample data.

## Contributors
- Abel Lefika Molefhi ([@abelmolefhi](https://github.com/abelmolefhi))
- Edward Gabathusi ([@237-eddie](https://github.com/237-eddie))
- Philipp Schöneberg ([@philippschoeneberg](https://github.com/philippschoeneberg))
- Thapelo Segosebe
