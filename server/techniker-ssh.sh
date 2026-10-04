#!/bin/sh
# SSH-Zugang für Techniker: ein gemeinsamer Benutzer "fern" ohne Shell, der nur die Weiterleitung
# zum Techniker-Port des Dienstes öffnen darf. Je Techniker eine Zeile in authorized_keys.
#
#   techniker-ssh.sh hinzu <name> '<öffentlicher Schlüssel>'
#   techniker-ssh.sh weg <name>
#   techniker-ssh.sh liste
set -eu
BENUTZER=fern
PORT=8811
DATEI=/home/$BENUTZER/.ssh/authorized_keys

id "$BENUTZER" >/dev/null 2>&1 || useradd --create-home --shell /usr/sbin/nologin "$BENUTZER"
install -d -m 700 -o "$BENUTZER" -g "$BENUTZER" "/home/$BENUTZER/.ssh"
touch "$DATEI"; chown "$BENUTZER:$BENUTZER" "$DATEI"; chmod 600 "$DATEI"

case "${1:-}" in
  hinzu)
    name=$2; schluessel=$3
    echo "$name" | grep -Eq '^[a-z0-9-]+$' || { echo "Name: nur Kleinbuchstaben, Ziffern, Bindestrich"; exit 1; }
    echo "$schluessel" | grep -Eq '^ssh-ed25519 [A-Za-z0-9+/=]+( .*)?$' || { echo "Erwartet wird ein ssh-ed25519-Schlüssel"; exit 1; }
    kern=$(echo "$schluessel" | awk '{print $1" "$2}')
    grep -v " techniker:$name\$" "$DATEI" > "$DATEI.neu" || true
    echo "restrict,port-forwarding,permitopen=\"127.0.0.1:$PORT\" $kern techniker:$name" >> "$DATEI.neu"
    cat "$DATEI.neu" > "$DATEI"; rm "$DATEI.neu"
    echo "SSH-Zugang für $name eingetragen."
    ;;
  weg)
    grep -v " techniker:$2\$" "$DATEI" > "$DATEI.neu" || true
    cat "$DATEI.neu" > "$DATEI"; rm "$DATEI.neu"
    echo "SSH-Zugang für $2 entfernt."
    ;;
  liste)
    awk '{print $NF}' "$DATEI"
    ;;
  *)
    sed -n '2,8p' "$0"; exit 1
    ;;
esac
