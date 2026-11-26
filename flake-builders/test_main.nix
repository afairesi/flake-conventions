{
  inputs,
  pkgs,
  system,
}:
let
  inherit (import ./test_fixtures.nix { inherit inputs pkgs system; })
    consumer
    htmlPackage
    overridden
    pythonLibrary
    ;
  accepts =
    builder: attributes:
    (builtins.tryEval (
      builder (
        {
          inherit pkgs;
          executable = true;
        }
        // attributes
      )
    )).success;
  apps = consumer.apps.${system};
  check = expected: expr: { inherit expected expr; };
  checks = consumer.checks.${system};
  empty = inputs.self.lib.mkFlake {
    inputs.self = empty // {
      outPath = consumer.outPath + "/unavailable";
    };
    systems = [ system ];
  };
  packages = consumer.packages.${system};
  shells = consumer.devShells.${system};
in
{
  testAppsExposePackageMetadata = check true (
    apps.example.type == "app"
    && apps.example.meta == packages.example.meta
    && apps.example.meta.description == "Afairesi consumer contract example"
  );
  testAppsWithoutADescriptionUseRunFollowedByThePackageName =
    check "Run example"
      overridden.apps.${system}.example.meta.description;
  testBuildersDeclareTheirPackageKind =
    check
      [ "python" "bash" "html" "lean4" ]
      [
        packages.example.afairesiKind
        packages.bash-example.afairesiKind
        htmlPackage.afairesiKind
        packages.lean-example.afairesiKind
      ];
  testChecksIncludeTheFormatterAndAllPackages = check true (
    checks.formatter == consumer.formatter.${system}.passthru.tests.check
    && pkgs.lib.all (name: checks."pkgs-${name}" == packages.${name}) (builtins.attrNames packages)
  );
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
        (packages.test_main.meta ? mainProgram)
      ];
  testLibraryBuildersRejectRuntimeHooks =
    check
      [ false false ]
      [
        (accepts inputs.self.lib.mkPythonPackage {
          executable = false;
          runtimeHook = "echo invalid";
          src = ./fixtures/example;
        })
        (accepts inputs.self.lib.mkLean4Package {
          executable = false;
          runtimeHook = "echo invalid";
          src = ./fixtures/lean-example;
        })
      ];
  testNativePackagesHaveNoFrameworkKind =
    check
      [ false false ]
      [
        (packages.python_native ? afairesiKind)
        (packages.rust-native ? afairesiKind)
      ];
  testPackageBuildersReceiveConsumerConfigurationAndOverlays = check {
    afairesiContract = true;
    allowUnfree = true;
    cudaSupport = true;
  } packages.example.consumerConfig;
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
        packages.example.meta.mainProgram
        packages.lean-example.meta.mainProgram
      ];
  testPythonBuilderRequiresImportableModuleNames = check false (
    pkgs.lib.any (src: accepts inputs.self.lib.mkPythonPackage { inherit src; }) [
      "/invalid/123"
      "/invalid/class"
    ]
  );
  testPythonBuilderReservesThePackageInitializer = check false (
    accepts inputs.self.lib.mkPythonPackage {
      src = ./fixtures/python_native;
    }
  );
  testPythonCheckDependenciesAreAvailableToChecksAndShells = check true (
    pkgs.lib.all
      (
        input:
        builtins.elem input overridden.checks.${system}.example.nativeBuildInputs
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
        (packages.example.drvAttrs ? runtimeHook)
        (packages.lean-example.drvAttrs ? runtimeHook)
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
