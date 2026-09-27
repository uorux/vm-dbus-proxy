{ pkgs }:
pkgs.stdenv.mkDerivation {
  pname = "vm-capture";
  version = "0.1.0";
  src = ./.;
  nativeBuildInputs = [ pkgs.pkg-config ];
  buildInputs = [
    pkgs.pipewire
    pkgs.libdrm
    pkgs.libglvnd
    pkgs.libgbm
  ];
  buildPhase = ''
    $CC -std=gnu11 -O2 -Wall -Wextra vm-capture.c -o vm-capture $(pkg-config --cflags --libs libpipewire-0.3 libdrm egl gbm)
  '';
  doCheck = true;
  checkPhase = ''
    $CC -std=gnu11 -O2 -Wall -Wextra -Werror test-protocol.c -o test-protocol $(pkg-config --cflags --libs libpipewire-0.3 libdrm egl gbm)
    ./test-protocol
  '';
  installPhase = ''
    install -Dm755 vm-capture $out/bin/vm-capture
  '';
}
