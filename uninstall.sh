#/usr/bin/env bash

systemctl stop doh-forwarder > /dev/null 2>&1 &&
systemctl disable doh-forwarder > /dev/null 2>&1 &&
systemctl daemon-reload &&
echo "doh-forwarder disabled as a service" ||
echo "doh-forwarder not disabled as a service, are you running with super user permissions, is it installed as a service?"

rm -f /usr/local/bin/doh-forwarder
rm -f /etc/systemd/system/doh-forwarder.service

[ ! -f /usr/local/bin/doh-forwarder ] &&
[ ! -f /etc/systemd/system/doh-forwarder.service ] &&
echo "doh-forwarder uninstalled" ||
echo "doh-forwarder not uninstalled, are you running with super user permissions?"
