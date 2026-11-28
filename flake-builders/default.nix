{
  inputs,
  pkgs,
  pname,
  system,
  ...
}:
let
  inherit (import ./test_fixtures.nix { inherit inputs pkgs system; }) consumer;
in
pkgs.stdenvNoCC.mkDerivation {
  inherit pname;
  dontBuild = true;
  installPhase = ''
    runHook preInstall
    mkdir -p "$out"
    cp main.nix "$out/"
    runHook postInstall
  '';
  meta.description = "Nix library for Afairesi packages, checks, formatters, and hosts";
  passthru.tests = {
    consumer = import ./test_consumer.nix { inherit inputs pkgs system; };
  }
  // pkgs.lib.optionalAttrs (consumer.checks.${system} ? exampleVm) {
    inherit (consumer.checks.${system}) exampleVm;
  };
  src = ./.;
  version = "0.0.0";
}
