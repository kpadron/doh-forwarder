#/usr/bin/env bash

systemctl stop doh-forwarder
systemctl disable doh-forwarder
cp doh-async.py /usr/local/bin/doh-forwarder
cp doh-forwarder.service /etc/systemd/system/
systemctl daemon-reload
systemctl start doh-forwarder
systemctl enable doh-forwarder
