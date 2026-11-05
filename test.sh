#!/bin/bash
set -e

API="http://127.0.0.1:8000"
sleep 2

echo "[*] Testing API availability..."
curl -s -f "$API/" > /dev/null
echo "[+] API is reachable"

echo "[*] Testing image registration..."
curl -s -X POST "$API/api/images" \
  -H "Content-Type: application/json" \
  -d '{"name": "test-alpine", "path": "/home/ivan/cloudmaker/images/test.img", "description": "Alpine Test"}' | grep "registered" > /dev/null
echo "[+] Image registered"

echo "[*] Testing network creation..."
curl -s -X POST "$API/api/networks" \
  -H "Content-Type: application/json" \
  -d '{"name": "test-net", "bridge_name": "virbr10", "dhcp_start": "192.168.150.10", "dhcp_end": "192.168.150.50"}' | grep "created" > /dev/null
virsh net-info test-net > /dev/null
echo "[+] Virtual network active"

echo "[*] Provisioning virtual machine..."
curl -s -X POST "$API/api/vms" \
  -H "Content-Type: application/json" \
  -d '{"name": "auto-vm-01", "vcpus": 2, "ram_mb": 2048, "disk_size_gb": 10, "image_name": "ubuntu-24.04"}' | grep "created" > /dev/null
echo "[+] VM created successfully"

echo "[*] Testing quota limits..."
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$API/api/vms" \
  -H "Content-Type: application/json" \
  -d '{"name": "oversized-vm", "vcpus": 8, "ram_mb": 4096, "disk_size_gb": 10, "image_name": "ubuntu-24.04"}')

if [ "$HTTP_CODE" -eq 400 ]; then
  echo "[+] Quota enforcement verified (HTTP 400)"
else
  echo "[-] Quota test failed: HTTP $HTTP_CODE"
  exit 1
fi

echo "[*] Checking Prometheus metrics..."
curl -s "$API/metrics" | grep "cloudmaker_allocated_vcpus" > /dev/null
echo "[+] Metrics exported"

echo "[*] Testing persistence across restart..."
docker restart cloudmaker-api > /dev/null
sleep 3
RUNNING_STATE=$(curl -s "$API/api/vms" | grep -o '"name":"auto-vm-01"' || true)
if [ -n "$RUNNING_STATE" ]; then
  echo "[+] State persisted after service restart"
else
  echo "[-] VM state lost"
  exit 1
fi

echo "[*] Tearing down test VM..."
curl -s -X POST "$API/api/vms/auto-vm-01/stop" | grep "stopped" > /dev/null
curl -s -X DELETE "$API/api/vms/auto-vm-01" | grep "deleted" > /dev/null
echo "[+] VM removed"
virsh net-destroy test-net >/dev/null 2>&1 || true
virsh net-undefine test-net >/dev/null 2>&1 || true

echo ""
echo "All integration tests passed."
