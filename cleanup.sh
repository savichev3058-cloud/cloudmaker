#!/bin/bash
set -e

for vm in $(virsh list --all --name | grep -v '^$'); do
    virsh destroy "$vm" 2>/dev/null || true
    virsh undefine "$vm" 2>/dev/null || true
done

for net in $(virsh net-list --all --name | grep -v '^$' | grep -v 'default'); do
    virsh net-destroy "$net" 2>/dev/null || true
    virsh net-undefine "$net" 2>/dev/null || true
done

rm -f ~/cloudmaker/vms/*.qcow2
rm -f ~/cloudmaker/configs/*-seed.iso
rm -f ~/cloudmaker/configs/*-*.yaml

echo "Environment cleanup complete."
