{ inputs, pkgs }:
let
  inherit (import ./test_fixtures.nix { inherit inputs pkgs; })
    consumer
    consumerSource
    htmlPackage
    overridden
    package
    pythonLibrary
    system
    ;
  accepts =
    attributes:
    (builtins.tryEval (
      inputs.self.lib.mkPythonPackage (
        {
          inherit pkgs;
          executable = true;
          src = ./fixtures/example;
        }
        // attributes
      )
    )).success;
  acceptsLean =
    attributes:
    (builtins.tryEval (
      inputs.self.lib.mkLean4Package (
        {
          inherit pkgs;
          executable = true;
          src = ./fixtures/lean-example;
        }
        // attributes
      )
    )).success;
  apps = consumer.apps.${system};
  check = expected: expr: { inherit expected expr; };
  checks = consumer.checks.${system};
  empty = inputs.self.lib.mkFlake {
    inputs.self = empty // {
      outPath = consumerSource + "/unavailable";
    };
    systems = [ system ];
  };
  leanExample = consumer.packages.${system}.lean-example;
  leanLibrary = consumer.packages.${system}.test_main;
  overriddenCheck = overridden.checks.${system}.example;
  packages = consumer.packages.${system};
  shells = consumer.devShells.${system};
in
{
  testAppsExposePackageMetadata = check true (
    apps.example.type == "app"
    && apps.example.meta == package.meta
    && apps.example.meta.description == "Afairesi consumer contract example"
  );
  testAppsWithoutADescriptionUseRunFollowedByThePackageName =
    check "Run example"
      overridden.apps.${system}.example.meta.description;
  testBuildersDeclareTheirPackageKind =
    check
      [ "python" "bash" "html" "lean4" ]
      [
        package.afairesiKind
        packages.bash-example.afairesiKind
        htmlPackage.afairesiKind
        leanExample.afairesiKind
      ];
  testChecksIncludeTheFormatterAndAllPackages = check true (
    checks.formatter == consumer.formatter.${system}.passthru.tests.check
    && pkgs.lib.all (name: checks."pkgs-${name}" == packages.${name}) (builtins.attrNames packages)
  );
  testDefaultPackageIsTheLayoutLibrary =
    check inputs.self.packages.${system}.flake_layout
      inputs.self.packages.${system}.default;
  testEachTestShellHasAMatchingCheck = check true (
    pkgs.lib.all (name: checks."devshell-${name}" == shells.${name}) (builtins.attrNames shells)
  );
  testEmptyRepositoriesExposeOnlyRepositoryChecksAndAFormatter =
    check
      {
        apps = { };
        checks = [
          "formatter"
          "layout"
          "lint"
        ];
        formatter = [ system ];
        hosts = { };
        packages = { };
        shells = { };
      }
      {
        apps = empty.apps.${system};
        checks = builtins.attrNames empty.checks.${system};
        formatter = builtins.attrNames empty.formatter;
        hosts = empty.nixosConfigurations;
        packages = empty.packages.${system};
        shells = empty.devShells.${system};
      };
  testEveryPackageSystemHasAFormatter = check (builtins.attrNames consumer.packages) (
    builtins.attrNames consumer.formatter
  );
  testHostAndVmChecksUseTheHostPlatform =
    check
      [
        (system == "x86_64-linux")
        (system == "x86_64-linux")
      ]
      [
        (checks ? nixos-example)
        (checks ? exampleVm)
      ];
  testHostsReceiveConsumerConfigurationAndOverlays =
    let
      host = consumer.nixosConfigurations.example;
    in
    check
      [ true true true "example" ]
      [
        host.pkgs.config.allowUnfree
        host.pkgs.config.cudaSupport
        host.pkgs.afairesiContract
        host.config.networking.hostName
      ];
  testHtmlPackagesPreserveTheSuppliedDescription = check "Source listing" htmlPackage.meta.description;
  testLeanLibraryHasNoRunnableApp = check false (apps ? test_main);
  testLibrariesHaveNoMainProgram =
    check
      [ false false ]
      [
        (pythonLibrary.meta ? mainProgram)
        (leanLibrary.meta ? mainProgram)
      ];
  testLibraryBuildersRejectRuntimeHooks =
    check
      [ false false ]
      [
        (accepts {
          executable = false;
          runtimeHook = "echo invalid";
        })
        (acceptsLean {
          executable = false;
          runtimeHook = "echo invalid";
        })
      ];
  testNativeBuildersKeepTheirOwnPackageAttributes =
    check
      [ false false true ]
      [
        (packages.python_native ? afairesiKind)
        (packages.rust-native ? afairesiKind)
        (packages.python_native.example == package)
      ];
  testPackageBuildersReceiveConsumerConfigurationAndOverlays = check {
    afairesiContract = true;
    allowUnfree = true;
    cudaSupport = true;
  } package.consumerConfig;
  testPackageChecksExposeSupportedPassthruTestsAndOmitUnavailableOnes = check true (
    checks.pkgs-python_native-contract == packages.python_native.passthru.tests.contract
    && !(overridden.checks.${system} ? pkgs-example-unavailable)
  );
  testPackagesWithTestEnvironmentsExposeShells = check [
    "bash-example-test"
    "example-test"
    "lean-example-test"
    "test_main-test"
  ] (builtins.attrNames shells);
  testPythonAndLeanExecutablesUseTheirPackageNameAsTheMainProgram =
    check
      [ "example" "lean-example" ]
      [
        package.meta.mainProgram
        leanExample.meta.mainProgram
      ];
  testPythonBuilderRequiresImportableModuleNames = check false (
    accepts { src = "/invalid/123"; } || accepts { src = "/invalid/class"; }
  );
  testPythonBuilderReservesThePackageInitializer = check false (accepts {
    src = ./fixtures/python_native;
  });
  testPythonCheckDependenciesAreAvailableToChecksAndShells = check true (
    pkgs.lib.all
      (
        input:
        builtins.elem input overriddenCheck.nativeBuildInputs
        && builtins.elem input overridden.devShells.${system}.example-test.nativeBuildInputs
      )
      [
        pkgs.python3Packages.six
        pkgs.python3Packages.colorama
      ]
  );
  testPythonLibraryCheckOmitsTheExecutablePath = check false (
    inputs.self.checks.${system}.nix_syntax ? PACKAGE_E2E_EXECUTABLE
  );
  testRuntimeHooksStayOutsideThePackageBuild =
    check
      [ false false ]
      [
        (package.drvAttrs ? runtimeHook)
        (leanExample.drvAttrs ? runtimeHook)
      ];
  testSuiteChecksRequireAPackageWithAMatchingSuite =
    check
      [ true false false false ]
      [
        (checks ? example)
        (checks ? orphan)
        (checks ? nix-untested)
        (checks ? python_native)
      ];
  testUnsupportedPackagesAreAbsentFromBothPackagesAndChecks =
    check
      [ false false false ]
      [
        (packages ? unavailable)
        (checks ? unavailable)
        (checks ? pkgs-unavailable)
      ];
}
