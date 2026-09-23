#!/bin/bash
cd /home/site/wwwroot/backend
gunicorn -w 4 -k uvicorn.workers.UvicornWorker server:app --bind 0.0.0.0:8000
