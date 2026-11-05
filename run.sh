#!/bin/bash
set -e

docker compose up -d --build
echo "CloudMaker API запущен."
echo "Панель управления: http://$(hostname -I | awk '{print $1}'):8000/"
