#/usr/bin/env bash

systemctl stop doh-forwarder > /dev/null 2>&1
systemctl disable doh-forwarder > /dev/null 2>&1
cp "$1" /usr/local/bin/doh-forwarder &&
chmod 755 /usr/local/bin/doh-forwarder &&
cp doh-forwarder.service /etc/systemd/system/doh-forwarder.service &&
chmod 644 /etc/systemd/system/doh-forwarder.service &&
echo "doh-forwarder installed" ||
(echo "doh-forwarder not installed, undoing changes" &&
./uninstall.sh)

systemctl daemon-reload &&
systemctl start doh-forwarder &&
systemctl enable doh-forwarder &&
echo "doh-forwarder enabled as a service" ||
echo "doh-forwarder not enabled as a service"
