{ pkgs }:
let
  source = builtins.path {
    path = ./src;
    name = "vm-dbus-proxy-source";
    filter = path: type: baseNameOf path != "__pycache__";
  };
in
pkgs.writeScriptBin "vm-dbus-proxy" ''
  #!${pkgs.python3}/bin/python3 -IS
  import sys
  sys.path.insert(0, "${source}")
  from vm_dbus_proxy.proxy import main
  main()
''
