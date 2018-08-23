#/usr/bin/env bash

systemctl stop doh-forwarder
systemctl disable doh-forwarder
rm /usr/local/bin/doh-forwarder
rm /etc/systemd/system/doh-forwarder.service
systemctl daemon-reload
