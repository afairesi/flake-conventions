{ pkgs, pname, ... }:
pkgs.rustPlatform.buildRustPackage (finalAttrs: {
  inherit pname;
  cargoHash = "sha256-YXOCy2tgOYFeGqbOfPMlMHBIy3g2pBIEov+UYzq9030=";
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
    hash = "sha256-Os4fDWYzuLrtW4yvawpJ17mNMmDNtLmmvGltbJWlczM=";
  };
  version = "0.5.1";
})
