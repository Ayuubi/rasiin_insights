from setuptools import setup, find_packages

with open("requirements.txt") as f:
	install_requires = f.read().strip().split("\n")

# get version from __version__ variable in rasiin_insights/__init__.py
from rasiin_insights import __version__ as version

setup(
	name="rasiin_insights",
	version=version,
	description="Management dashboards and reporting",
	author="Rasiin Technology",
	author_email="rasiintech@gmail.com",
	packages=find_packages(),
	zip_safe=False,
	include_package_data=True,
	install_requires=install_requires
)
