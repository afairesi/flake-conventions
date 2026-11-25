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
      inherit (import ./flake-builders/main.nix inputs) lib;
    in
    lib.mkFlake { inherit inputs; } // { inherit lib; };
}
