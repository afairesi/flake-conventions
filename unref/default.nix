{ pkgs, pname, ... }:
pkgs.rustPlatform.buildRustPackage (finalAttrs: {
  inherit pname;
  cargoHash = "sha256-0bBYzWIijkFOt77WgU4lRX3w1SZks3buk2tE4LhkQhw=";
  doInstallCheck = true;
  env = pkgs.lib.optionalAttrs (pkgs.stdenv.hostPlatform.system == "aarch64-linux") {
    JEMALLOC_SYS_WITH_LG_PAGE = 16;
  };
  meta = {
    description = "Find dead code in Python projects";
    homepage = "https://github.com/Apakottur/unref";
    license = pkgs.lib.licenses.mit;
    mainProgram = pname;
  };
  nativeInstallCheckInputs = [ pkgs.versionCheckHook ];
  src = pkgs.fetchPypi {
    inherit (finalAttrs) pname version;
    hash = "sha256-rSqCbcXhXjBRlWOyr4IzgRF+AWMYTKXr2geEVEHXej4=";
  };
  version = "0.6.1";
})
