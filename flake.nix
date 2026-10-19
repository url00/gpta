{
  description = "GPTA - modular receipt data server for thermal printers";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in {
      packages = forAllSystems (pkgs: {
        default = pkgs.stdenvNoCC.mkDerivation {
          pname = "gpta";
          version = "0.1.0";
          src = ./.;
          nativeBuildInputs = [ pkgs.makeWrapper ];
          installPhase = ''
            mkdir -p $out/lib/gpta $out/bin
            cp -r gpta modules web $out/lib/gpta/
            makeWrapper ${pkgs.python3}/bin/python3 $out/bin/gpta \
              --add-flags "-m gpta" \
              --set PYTHONPATH "$out/lib/gpta"
          '';
          meta = {
            description = "Modular receipt data server and printer simulator";
            platforms = systems;
          };
        };
      });

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [ pkgs.python3 pkgs.curl pkgs.git ];
        };
      });

      apps = forAllSystems (pkgs: {
        default = {
          type = "app";
          program = "${self.packages.${pkgs.system}.default}/bin/gpta";
        };
      });
    };
}
