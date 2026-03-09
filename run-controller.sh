
#!/usr/bin/env bash
uvicorn controller.api:app --host 0.0.0.0 --port 8000 --reload --no-server-header
