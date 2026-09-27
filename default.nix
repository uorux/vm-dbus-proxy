{ pkgs }:
let
  source = builtins.path {
    path = ./src;
    name = "vm-dbus-proxy-source";
    filter = path: type: baseNameOf path != "__pycache__";
  };
  entry =
    name: module:
    pkgs.writeScriptBin name ''
      #!${pkgs.python3}/bin/python3 -IS
      import sys
      sys.path.insert(0, "${source}")
      from vm_dbus_proxy.${module} import main
      main()
    '';
in
pkgs.symlinkJoin {
  name = "vm-dbus-proxy";
  paths = [
    (entry "vm-dbus-proxy" "proxy")
    (entry "vm-dbus-capture-proxy" "capture_proxy")
    (entry "vm-capture-broker" "capture_broker")
    (import ./capture { inherit pkgs; })
  ];
}
