{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    treefmt-nix = {
      inputs.nixpkgs.follows = "nixpkgs";
      url = "github:numtide/treefmt-nix";
    };
  };
  outputs =
    inputs:
    let
      inherit ((import ./flake-builders/main.nix inputs)) lib;
      flake = lib.mkFlake { inherit inputs; };
    in
    flake
    // {
      inherit lib;
      packages = builtins.mapAttrs (
        _: packages: packages // { default = packages.flake_layout; }
      ) flake.packages;
    };
}
