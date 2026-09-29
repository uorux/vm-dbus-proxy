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
  # The unit tests gate the package: the host adapter is part of the host's
  # boundary with the guest. The integration test uses a private dbus-daemon.
  tests =
    pkgs.runCommand "vm-dbus-proxy-tests"
      {
        nativeBuildInputs = [
          pkgs.python3
          pkgs.dbus
        ];
        tests = builtins.path {
          path = ./tests;
          name = "vm-dbus-proxy-tests-source";
          filter = path: type: baseNameOf path != "__pycache__";
        };
      }
      ''
        cp -r "$tests" tests
        chmod -R u+w tests
        if ! PYTHONPATH=${source} python3 -m unittest discover -s tests -v > test.log 2>&1; then
          cat test.log
          exit 1
        fi
        tail -n 3 test.log
        mkdir -p "$out"
      '';
in
pkgs.symlinkJoin {
  name = "vm-dbus-proxy";
  paths = [
    (entry "vm-dbus-proxy" "proxy")
    (entry "vm-dbus-capture-proxy" "capture_proxy")
    (entry "vm-capture-broker" "capture_broker")
    (import ./capture { inherit pkgs; })
    tests
  ];
}
