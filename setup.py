"""Include sibling runtime resources without duplicating their source files."""
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py

RESOURCES = ("now_playing.ax", "now_playing-v2.2.ax", "now_playing-v2.3.ax", "drift_ecosystem.json", "drift_ecosystem.schema.json")


class RuntimeBuild(build_py):
    def get_source_files(self):
        return super().get_source_files() + list(RESOURCES)

    def find_package_modules(self, package, package_dir):
        return [item for item in super().find_package_modules(package, package_dir)
                if not item[1].startswith("test_")]

    def run(self):
        super().run()
        for name in RESOURCES:
            self.copy_file(name, str(Path(self.build_lib) / name))

    def get_outputs(self, include_bytecode=1):
        return super().get_outputs(include_bytecode) + [str(Path(self.build_lib) / name) for name in RESOURCES]


setup(cmdclass={"build_py": RuntimeBuild})
