import contextlib
import json
import os
import pyarrow
import subprocess
from subprocess import check_output
import sys
import shutil
import tempfile
from unittest import mock
import zipfile

import pytest

from pex.pex_info import PexInfo

from cluster_pack import packaging, filesystem


MODULE_TO_TEST = "cluster_pack.packaging"
MYARCHIVE_FILENAME = "myarchive.pex"
MYARCHIVE_METADATA = "myarchive.json"
VARNAME = 'VARNAME'


def test_get_virtualenv_name():
    with mock.patch.dict('os.environ'):
        os.environ[VARNAME] = '/path/to/my_venv'
        assert 'my_venv' == packaging.get_env_name(VARNAME)


def test_get_virtualenv_empty_returns_default():
    with mock.patch.dict('os.environ'):
        if VARNAME in os.environ:
            del os.environ[VARNAME]
        assert 'default' == packaging.get_env_name(VARNAME)


def test_get_empty_editable_requirements():
    with tempfile.TemporaryDirectory() as tempdir:
        _create_venv(tempdir)
        subprocess.check_call([
                        f"{tempdir}/bin/python", "-m", "pip", "install",
                        "cloudpickle", _get_editable_package_name(), "pip==18.1"
                        ])
        editable_requirements = packaging._get_editable_requirements(f"{tempdir}/bin/python")
        assert len(editable_requirements) == 0


def test_get_empty_non_editable_requirements():
    with tempfile.TemporaryDirectory() as tempdir:
        _create_venv(tempdir)
        subprocess.check_call([
                    f"{tempdir}/bin/python", "-m", "pip", "install",
                    "-e", _get_editable_package_name(), "pip==18.1"
                    ])
        non_editable_requirements = packaging.get_non_editable_requirements(
            f"{tempdir}/bin/python")
        assert len(non_editable_requirements) == 0


def test__get_editable_requirements():
    with tempfile.TemporaryDirectory() as tempdir:
        _create_venv(tempdir)
        _pip_install(tempdir)
        editable_requirements = packaging._get_editable_requirements(f"{tempdir}/bin/python")
        assert len(editable_requirements) == 1
        assert os.path.basename(editable_requirements[0]) == "user_lib"


def test_get_non_editable_requirements():
    with tempfile.TemporaryDirectory() as tempdir:
        _create_venv(tempdir)
        _pip_install(tempdir)
        non_editable_requirements = packaging.get_non_editable_requirements(
            f"{tempdir}/bin/python")
        assert len(non_editable_requirements) == 1
        assert non_editable_requirements[0]["name"] == "cloudpickle"


def _create_venv(tempdir: str):
    subprocess.check_call([sys.executable, "-m", "venv", f"{tempdir}"])


def _pip_install(tempdir: str):
    subprocess.check_call([f"{tempdir}/bin/python", "-m", "pip", "install",
                           "cloudpickle", "pip==18.1"])
    pkg = _get_editable_package_name()
    subprocess.check_call([f"{tempdir}/bin/python", "-m", "pip", "install", "-e", pkg])
    if pkg not in sys.path:
        sys.path.append(pkg)


def _get_editable_package_name():
    return os.path.join(os.path.dirname(__file__), "user-lib")


def test_update_no_archive():
    map_is_exist = {MYARCHIVE_FILENAME: False}
    mock_fs = mock.MagicMock()
    mock_fs.exists = lambda arg: map_is_exist[arg]
    assert not packaging._is_archive_up_to_date(MYARCHIVE_FILENAME, [], mock_fs)


def test_update_no_metadata():
    map_is_exist = {MYARCHIVE_FILENAME: True,
                    MYARCHIVE_METADATA: False}
    mock_fs = mock.MagicMock()
    mock_fs.exists = lambda arg: map_is_exist[arg]
    assert not packaging._is_archive_up_to_date(MYARCHIVE_FILENAME, [], mock_fs)


@pytest.mark.parametrize("current_packages, metadata_packages, expected", [
    pytest.param({"a": "2.0", "b": "1.0"}, {"a": "2.0", "b": "1.0"}, True),
    pytest.param({"a": "2.0", "b": "1.0"}, {"a": "1.0", "b": "1.0"}, False),
    pytest.param({"a": "2.0", "b": "1.0"}, {"a": "2.0"}, False),
    pytest.param({"a": "2.0"}, {"a": "2.0", "b": "1.0"}, False),
    pytest.param({}, {"a": "2.0", "b": "1.0"}, False),
    pytest.param({"a": "2.0"}, {"c": "1.0"}, False),
    pytest.param({}, {}, True),
])
def test_update_version_comparaison(current_packages, metadata_packages,
                                    expected):

    map_is_exist = {MYARCHIVE_FILENAME: True,
                    MYARCHIVE_METADATA: True}

    mock_fs = mock.MagicMock()
    mock_fs.exists = lambda arg: map_is_exist[arg]

    with mock.patch.object(mock_fs, 'open',
        mock.mock_open(read_data=json.dumps(metadata_packages))
    ):
        assert packaging._is_archive_up_to_date(MYARCHIVE_FILENAME,
                                                current_packages, mock_fs) == expected


def Any(cls):
    class Any(cls):
        def __eq__(self, other):
            return isinstance(other, cls)
    return Any()


expected_file = """\
{
    "a": "1.0",
    "b": "2.0"
}"""


def test_dump_metadata():
    mock_fs = mock.Mock()
    mock_fs.rm.return_value = True
    mock_fs.exists.return_value = True
    mock_open = mock.mock_open()
    with mock.patch.object(mock_fs, 'open', mock_open):
        mock_fs.exists.return_value = True
        packages = {"a": "1.0", "b": "2.0"}
        packaging._dump_archive_metadata(
            MYARCHIVE_FILENAME,
            packages,
            filesystem.EnhancedFileSystem(mock_fs))
        # Check previous file has been deleted
        mock_fs.rm.assert_called_once_with(MYARCHIVE_METADATA)
        mock_open().write.assert_called_once_with(b'{\n    "a": "1.0",\n    "b": "2.0"\n}')


def test_upload_env():
    with contextlib.ExitStack() as stack:
        # Mock all objects
        mock_is_archive = stack.enter_context(
                mock.patch(f"{MODULE_TO_TEST}._is_archive_up_to_date"))
        mock_get_packages = stack.enter_context(
                mock.patch(f"{MODULE_TO_TEST}.get_non_editable_requirements"))

        mock_resolve_fs = stack.enter_context(
            mock.patch(f"{MODULE_TO_TEST}.filesystem.resolve_filesystem_and_path"))
        mock_fs = mock.MagicMock()
        mock_resolve_fs.return_value = mock_fs, ""

        stack.enter_context(mock.patch(f"{MODULE_TO_TEST}._dump_archive_metadata"))
        stack.enter_context(mock.patch(f"{MODULE_TO_TEST}.shutil.rmtree"))
        mock_packer = stack.enter_context(
            mock.patch(f"{MODULE_TO_TEST}.pack_in_pex")
        )

        # Regenerate archive
        mock_is_archive.return_value = False
        mock_get_packages.return_value = [{"name": "a", "version": "1.0"},
                                          {"name": "b", "version": "2.0"}]

        mock_packer.return_value = MYARCHIVE_FILENAME

        packaging.upload_env(MYARCHIVE_FILENAME, packaging.PEX_PACKER)
        mock_packer.assert_called_once_with(
            {"a": "1.0", "b": "2.0"}, Any(str), []
        )
        mock_fs.put.assert_called_once_with(MYARCHIVE_FILENAME, MYARCHIVE_FILENAME)

        mock_packer.reset_mock()
        packaging.upload_env(
            MYARCHIVE_FILENAME, packaging.PEX_PACKER,
            additional_packages={"c": "3.0"},
            ignored_packages=["a"]
        )
        mock_packer.assert_called_once_with(
            {"c": "3.0", "b": "2.0"}, Any(str), ["a"]
        )


def test_upload_env_should_throw_error_if_wrong_extension():
    with pytest.raises(ValueError):
        packaging.upload_env("myarchive.tar.gz", packer=packaging.CONDA_PACKER)


def test_upload_zip():
    home_fs_path = '/user/j.doe'
    with mock.patch(
            f"{MODULE_TO_TEST}.filesystem.resolve_filesystem_and_path") as mock_resolve_fs:
        mock_fs = mock.MagicMock()
        mock_resolve_fs.return_value = mock_fs, ""
        with mock.patch(f"{MODULE_TO_TEST}.request") as mock_request:
            with mock.patch(f"{MODULE_TO_TEST}.tempfile") as mock_tempfile:

                mock_fs.exists.return_value = False
                mock_tempfile.TemporaryDirectory.return_value.__enter__.return_value = "/tmp"

                result = packaging.upload_zip(
                    "http://myserver/mypex.pex",
                    f"{home_fs_path}/blah.pex"
                )

                mock_request.urlretrieve.assert_called_once_with(
                    "http://myserver/mypex.pex",
                    "/tmp/mypex.pex")
                mock_fs.put.assert_any_call("/tmp/mypex.pex", f"{home_fs_path}/blah.pex")

                assert "/user/j.doe/blah.pex" == result


def test_upload_env_in_a_pex():
    home_path = '/home/j.doe'
    home_fs_path = '/user/j.doe'
    with contextlib.ExitStack() as stack:
        mock_running_from_pex = stack.enter_context(
            mock.patch(f"{MODULE_TO_TEST}._running_from_pex"))
        mock_running_from_pex.return_value = True
        mock_pex_filepath = stack.enter_context(
            mock.patch(f"{MODULE_TO_TEST}.get_current_pex_filepath"))
        mock_pex_filepath.return_value = f"{home_path}/myapp.pex"

        mock_resolve_fs = stack.enter_context(
            mock.patch(f"{MODULE_TO_TEST}.filesystem.resolve_filesystem_and_path"))
        mock_fs = mock.MagicMock()
        mock_resolve_fs.return_value = mock_fs, ""

        mock__get_archive_metadata_path = stack.enter_context(
            mock.patch(f"{MODULE_TO_TEST}._get_archive_metadata_path")
        )
        mock__get_archive_metadata_path.return_value = f"{home_fs_path}/blah.json"

        # metadata & pex already exists on fs
        mock_fs.exists.return_value = True

        mock_pex_info = stack.enter_context(
            mock.patch(f"{MODULE_TO_TEST}.PexInfo")
        )

        def _from_pex(arg):
            if arg == f'{home_path}/myapp.pex':
                return PexInfo({"code_hash": 1})
            else:
                return PexInfo({"code_hash": 2})

        mock_pex_info.from_pex.side_effect = _from_pex

        result = packaging.upload_env(f'{home_fs_path}/blah.pex')

        # Check copy pex to remote
        mock_fs.put.assert_any_call(
            f'{home_path}/myapp.pex',
            f'{home_fs_path}/blah.pex')
        # Check metadata has been cleaned
        mock_fs.rm.assert_called_once_with(f'{home_fs_path}/blah.json')
        # check envname
        assert 'myapp' == result[1]


def test_get_current_pex_filepath():
    mock_pex = mock.Mock()
    mock_pex.__file__ = './current_directory/filename.pex/.bootstrap/_pex/__init__.pyc'
    sys.modules['_pex'] = mock_pex
    assert packaging.get_current_pex_filepath() == \
        os.path.join(os.getcwd(), 'current_directory/filename.pex')


def conda_is_available():
    p = subprocess.run(["conda"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
    return p.returncode == 0


@pytest.mark.skipif(not conda_is_available(), reason="conda is not available")
def test_create_conda_env():
    with tempfile.TemporaryDirectory() as tempdir:
        env_path = os.path.join(tempdir, "conda_env.zip")
        env_zip_path = packaging.create_and_pack_conda_env(
            env_path=env_path,
            reqs={"pycodestyle": "2.5.0"}
        )
        assert os.path.isfile(env_zip_path)
        env_path, _zip = os.path.splitext(env_zip_path)
        assert os.path.isdir(env_path)

        env_unzipped_path = os.path.join(tempdir, "conda_env_unzipped")
        with zipfile.ZipFile(env_zip_path) as zf:
            zf.extractall(env_unzipped_path)

        env_python_bin = os.path.join(env_unzipped_path, "bin", "python")
        os.chmod(env_python_bin, 0o755)
        check_output([env_python_bin, "-m", "pycodestyle", "--version"])


def test_get_editable_requirements():
    with mock.patch(f"{MODULE_TO_TEST}._running_from_pex") as mock_running_from_pex:
        mock_running_from_pex.return_value = True
        with tempfile.TemporaryDirectory() as tempdir:
            pkg = _get_editable_package_name()
            _create_editable_files(tempdir, os.path.basename(pkg))
            shutil.copytree(pkg, f"{tempdir}/{os.path.basename(pkg)}")

            editable_requirements = packaging.get_editable_requirements(
                editable_packages_dir=tempdir
            )
            assert editable_requirements == {os.path.basename(pkg): pkg}


def test_zip_path(tmpdir):
    s = "Hello, world!"
    tmpdir.mkdir("foo").join("bar.txt").write_text(s, encoding="utf-8")
    tmpdir.mkdir("py-lib").join("bar.py").write_text(s, encoding="utf-8")
    b = 0xffff.to_bytes(4, "little")
    tmpdir.join("boo.bin").write_binary(b)

    with tempfile.TemporaryDirectory() as tempdirpath:
        zipped_path = packaging.zip_path(str(tmpdir), False, tempdirpath)
        assert os.path.isfile(zipped_path)
        assert zipped_path.endswith(".zip")
        assert zipfile.is_zipfile(zipped_path)
        with zipfile.ZipFile(zipped_path) as zf:
            zipped = {zi.filename for zi in zf.filelist}
            assert "foo/bar.txt" in zipped
            assert "py-lib/bar.py" in zipped
            assert "boo.bin" in zipped

            assert zf.read("foo/bar.txt") == s.encode()
            assert zf.read("py-lib/bar.py") == s.encode()
            assert zf.read("boo.bin") == b


def _create_editable_files(tempdir, pkg):
    with open(f"{tempdir}/{packaging.EDITABLE_PACKAGES_INDEX}", "w") as file:
        for repo in [pkg, "not-existing-pgk"]:
            file.write(repo + "\n")