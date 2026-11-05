import os, subprocess, json, libvirt
from fastapi import FastAPI, HTTPException, status
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field
from typing import Optional

app = FastAPI(title="CloudMaker API", version="1.0.0")

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
IMAGES_DIR = os.path.join(BASE_DIR, "images")
VMS_DIR = os.path.join(BASE_DIR, "vms")
CONFIGS_DIR = os.path.join(BASE_DIR, "configs")
REGISTRY_FILE = os.path.join(IMAGES_DIR, "registry.json")

# Quotas
MAX_TOTAL_VCPUS = 8
MAX_TOTAL_RAM_MB = 8192

os.makedirs(IMAGES_DIR, exist_ok=True)
os.makedirs(VMS_DIR, exist_ok=True)
os.makedirs(CONFIGS_DIR, exist_ok=True)

if not os.path.exists(REGISTRY_FILE):
    init_reg = {
        "ubuntu-24.04": {
            "name": "ubuntu-24.04",
            "path": os.path.join(IMAGES_DIR, "ubuntu-24.04-minimal-cloudimg-amd64.img"),
            "description": "Ubuntu 24.04 LTS Minimal Cloud Image"
        }
    }
    with open(REGISTRY_FILE, "w") as f:
        json.dump(init_reg, f, indent=2)

def get_libvirt_conn():
    try:
        conn = libvirt.open("qemu:///system")
        if conn is None:
            raise HTTPException(status_code=500, detail="Failed to open connection to libvirt")
        return conn
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class VMCreateRequest(BaseModel):
    name: str = Field(..., pattern=r"^[a-zA-Z0-9_-]+$")
    vcpus: int = Field(default=1, ge=1, le=16)
    ram_mb: int = Field(default=1024, ge=512, le=16096)
    disk_size_gb: int = Field(default=10, ge=5, le=50)
    image_name: str = "ubuntu-24.04"
    network_name: str = "default"
    ssh_key: Optional[str] = None
    user_data: Optional[str] = None

class ImageRegisterRequest(BaseModel):
    name: str
    path: str
    description: str

class NetworkCreateRequest(BaseModel):
    name: str = Field(..., pattern=r"^[a-zA-Z0-9_-]+$")
    bridge_name: str = Field(..., pattern=r"^[a-zA-Z0-9_-]+$")
    dhcp_start: str = "192.168.150.2"
    dhcp_end: str = "192.168.150.254"

def check_quotas(conn, requested_vcpus: int, requested_ram_mb: int):
    total_vcpus, total_ram_kb = 0, 0
    for dom in conn.listAllDomains():
        info = dom.info()
        total_vcpus += info[3]
        total_ram_kb += info[1]
    current_ram_mb = total_ram_kb // 1024
    if (total_vcpus + requested_vcpus) > MAX_TOTAL_VCPUS:
        raise HTTPException(status_code=400, detail=f"CPU Quota exceeded: {total_vcpus}+{requested_vcpus} > {MAX_TOTAL_VCPUS}")
    if (current_ram_mb + requested_ram_mb) > MAX_TOTAL_RAM_MB:
        raise HTTPException(status_code=400, detail=f"RAM Quota exceeded: {current_ram_mb}+{requested_ram_mb}MB > {MAX_TOTAL_RAM_MB}MB")

@app.get("/api/vms")
def list_vms():
    conn = get_libvirt_conn()
    res = []
    st_names = {1: "running", 2: "blocked", 3: "paused", 4: "shutdown", 5: "shutoff", 6: "crashed"}
    for dom in conn.listAllDomains():
        info = dom.info()
        ips = []
        if dom.isActive():
            try:
                for iface in dom.interfaceAddresses(libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_LEASE).values():
                    for addr in iface.get("addrs", []):
                        ips.append(addr.get("addr"))
            except Exception:
                pass
        res.append({
            "name": dom.name(),
            "state": st_names.get(info[0], "unknown"),
            "vcpus": info[3],
            "ram_mb": info[1] // 1024,
            "ips": ips
        })
    conn.close()
    return res

@app.post("/api/vms", status_code=201)
def create_vm(req: VMCreateRequest):
    conn = get_libvirt_conn()
    try:
        conn.lookupByName(req.name)
        conn.close()
        raise HTTPException(status_code=400, detail=f"VM '{req.name}' already exists")
    except libvirt.libvirtError:
        pass

    with open(REGISTRY_FILE, "r") as f:
        registry = json.load(f)
    if req.image_name not in registry:
        conn.close()
        raise HTTPException(status_code=400, detail=f"Image '{req.image_name}' not found")

    base_image = registry[req.image_name]["path"]
    if not os.path.exists(base_image):
        conn.close()
        raise HTTPException(status_code=400, detail="Base image missing")

    check_quotas(conn, req.vcpus, req.ram_mb)

    vm_disk = os.path.join(VMS_DIR, f"{req.name}.qcow2")
    seed_iso = os.path.join(CONFIGS_DIR, f"{req.name}-seed.iso")
    meta_file = os.path.join(CONFIGS_DIR, f"{req.name}-meta.yaml")
    user_file = os.path.join(CONFIGS_DIR, f"{req.name}-user.yaml")

    with open(meta_file, "w") as f:
        f.write(f"instance-id: {req.name}\nlocal-hostname: {req.name}\n")

    if req.user_data:
        u_data = req.user_data
    else:
        ssh_block = f" ssh_authorized_keys:\n - {req.ssh_key}\n" if req.ssh_key else ""
        u_data = f"#cloud-config\nusers:\n - name: clouduser\n sudo: ALL=(ALL) NOPASSWD:ALL\n groups: sudo\n shell: /bin/bash\n lock_passwd: false\n{ssh_block}ssh_pwauth: true\nchpasswd:\n list: |\n clouduser:ubuntu\n expire: false\n"

    with open(user_file, "w") as f:
        f.write(u_data)

    try:
        subprocess.run(["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", base_image, vm_disk, f"{req.disk_size_gb}G"], check=True, capture_output=True)
        subprocess.run(["cloud-localds", seed_iso, user_file, meta_file], check=True, capture_output=True)
        subprocess.run([
            "virt-install", "--connect", "qemu:///system", "--name", req.name,
            "--memory", str(req.ram_mb), "--vcpus", str(req.vcpus), "--os-variant", "ubuntu22.04",
            "--disk", f"path={vm_disk},device=disk,bus=virtio",
            "--disk", f"path={seed_iso},device=disk,bus=virtio",
            "--network", f"network={req.network_name},model=virtio",
            "--graphics", "none", "--noautoconsole", "--import"
        ], check=True, capture_output=True)
    except subprocess.CalledProcessError as err:
        conn.close()
        raise HTTPException(status_code=500, detail=f"Provisioning error: {err.stderr.decode('utf-8')}")

    conn.close()
    return {"status": "created", "name": req.name}

@app.post("/api/vms/{name}/stop")
def stop_vm(name: str):
    conn = get_libvirt_conn()
    try:
        dom = conn.lookupByName(name)
        if dom.isActive():
            dom.destroy()
        conn.close()
        return {"status": "stopped", "name": name}
    except libvirt.libvirtError as e:
        conn.close()
        raise HTTPException(status_code=404, detail=str(e))

@app.post("/api/vms/{name}/start")
def start_vm(name: str):
    conn = get_libvirt_conn()
    try:
        dom = conn.lookupByName(name)
        if not dom.isActive():
            dom.create()
        conn.close()
        return {"status": "started", "name": name}
    except libvirt.libvirtError as e:
        conn.close()
        raise HTTPException(status_code=404, detail=str(e))

@app.delete("/api/vms/{name}")
def delete_vm(name: str):
    conn = get_libvirt_conn()
    try:
        dom = conn.lookupByName(name)
        if dom.isActive():
            dom.destroy()
        dom.undefine()
    except libvirt.libvirtError as e:
        conn.close()
        raise HTTPException(status_code=404, detail=str(e))
    conn.close()

    for p in [os.path.join(VMS_DIR, f"{name}.qcow2"), os.path.join(CONFIGS_DIR, f"{name}-seed.iso")]:
        if os.path.exists(p):
            os.remove(p)
    return {"status": "deleted", "name": name}

@app.get("/api/images")
def list_images():
    with open(REGISTRY_FILE, "r") as f:
        return json.load(f)

@app.post("/api/images")
def register_image(req: ImageRegisterRequest):
    with open(REGISTRY_FILE, "r") as f:
        data = json.load(f)
    data[req.name] = {"name": req.name, "path": req.path, "description": req.description}
    with open(REGISTRY_FILE, "w") as f:
        json.dump(data, f, indent=2)
    return {"status": "registered", "image": data[req.name]}

@app.get("/api/networks")
def list_networks():
    conn = get_libvirt_conn()
    res = [{"name": n.name(), "active": n.isActive(), "bridge": n.bridgeName()} for n in conn.listAllNetworks()]
    conn.close()
    return res

@app.post("/api/networks")
def create_network(req: NetworkCreateRequest):
    conn = get_libvirt_conn()
    xml = f"""
    <network>
      <name>{req.name}</name>
      <forward mode='nat'/>
      <bridge name='{req.bridge_name}' stp='on' delay='0'/>
      <ip address='192.168.150.1' netmask='255.255.255.0'>
        <dhcp>
          <range start='{req.dhcp_start}' end='{req.dhcp_end}'/>
        </dhcp>
      </ip>
    </network>
    """
    try:
        net = conn.networkDefineXML(xml)
        net.create()
        net.setAutostart(True)
    except libvirt.libvirtError as e:
        conn.close()
        raise HTTPException(status_code=400, detail=str(e))
    conn.close()
    return {"status": "created", "network": req.name, "bridge": req.bridge_name}

@app.get("/metrics", response_class=PlainTextResponse)
def get_metrics():
    conn = get_libvirt_conn()
    domains = conn.listAllDomains()
    total_cpu = sum(d.info()[3] for d in domains)
    total_ram = sum(d.info()[1] // 1024 for d in domains)
    lines = [
        f"cloudmaker_vms_total {len(domains)}",
        f"cloudmaker_vms_running {sum(1 for d in domains if d.isActive())}",
        f"cloudmaker_quota_vcpus_max {MAX_TOTAL_VCPUS}",
        f"cloudmaker_quota_ram_mb_max {MAX_TOTAL_RAM_MB}",
        f"cloudmaker_allocated_vcpus {total_cpu}",
        f"cloudmaker_allocated_ram_mb {total_ram}"
    ]
    conn.close()
    return "\n".join(lines) + "\n"

@app.get("/")
def get_ui():
    ui_path = os.path.join(BASE_DIR, "static", "index.html")
    if os.path.exists(ui_path):
        return FileResponse(ui_path)
    return {"message": "CloudMaker API running"}
