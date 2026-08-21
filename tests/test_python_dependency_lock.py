import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from probhub.dependency_lock import (
    DependencyLockError,
    LockedArtifact,
    SUPPORTED_DEPENDENCY_TARGETS,
    current_dependency_target,
    load_dependency_lock,
    parse_dependency_lock,
    parse_source_requirements,
    render_dependency_lock,
    validate_target_coverage,
    verify_wheelhouse,
    wheel_supports_target,
)
from scripts import update_python_dependency_lock as updater


SOURCE = (
    "Alpha_Pkg==1.2.3\n"
    "colorama==0.4.6; platform_system == \"Windows\"\n"
    "typing_extensions==4.16.0; python_version < \"3.11\"\n"
)
SHA_ALPHA_WINDOWS = "1" * 64
SHA_ALPHA_LINUX = "2" * 64
SHA_COLORAMA = "3" * 64
SHA_TYPING_EXTENSIONS = "4" * 64


def artifact(filename, sha256, size):
    return LockedArtifact(filename=filename, sha256=sha256, size=size)


def artifact_mapping(*, reverse=False):
    alpha = (
        artifact(
            "alpha_pkg-1.2.3-cp311-cp311-win_amd64.whl",
            SHA_ALPHA_WINDOWS,
            101,
        ),
        artifact(
            "alpha_pkg-1.2.3-cp311-cp311-manylinux_2_17_x86_64.whl",
            SHA_ALPHA_LINUX,
            202,
        ),
    )
    items = [
        ("alpha-pkg", tuple(reversed(alpha)) if reverse else alpha),
        (
            "colorama",
            (artifact("colorama-0.4.6-py2.py3-none-any.whl", SHA_COLORAMA, 303),),
        ),
        (
            "typing-extensions",
            (
                artifact(
                    "typing_extensions-4.16.0-py3-none-any.whl",
                    SHA_TYPING_EXTENSIONS,
                    404,
                ),
            ),
        ),
    ]
    if reverse:
        items.reverse()
    return dict(items)


def rendered_lock(*, reverse=False):
    return render_dependency_lock(SOURCE, artifact_mapping(reverse=reverse))


class DependencyLockParsingTests(unittest.TestCase):
    def test_current_target_accepts_only_the_declared_runtime_matrix(self):
        version = SimpleNamespace(major=3, minor=12)
        implementation = SimpleNamespace(name="cpython")
        with (
            patch("probhub.dependency_lock.sys.version_info", version),
            patch("probhub.dependency_lock.sys.implementation", implementation),
            patch("probhub.dependency_lock.platform.system", return_value="Linux"),
            patch("probhub.dependency_lock.platform.machine", return_value="x86_64"),
        ):
            self.assertEqual(
                current_dependency_target().identifier,
                "linux-x86_64-cp312",
            )

        unsupported = (
            (SimpleNamespace(major=3, minor=13), implementation, "Linux", "x86_64"),
            (version, SimpleNamespace(name="pypy"), "Linux", "x86_64"),
            (version, implementation, "Darwin", "x86_64"),
            (version, implementation, "Windows", "ARM64"),
        )
        for version_info, runtime, system, machine in unsupported:
            with (
                self.subTest(system=system, machine=machine, runtime=runtime.name),
                patch("probhub.dependency_lock.sys.version_info", version_info),
                patch("probhub.dependency_lock.sys.implementation", runtime),
                patch("probhub.dependency_lock.platform.system", return_value=system),
                patch("probhub.dependency_lock.platform.machine", return_value=machine),
                self.assertRaises(DependencyLockError) as caught,
            ):
                current_dependency_target()
            self.assertEqual(caught.exception.code, "dependency_runtime_unsupported")

    def test_source_and_lock_have_exact_pin_parity_and_preserve_markers(self):
        pins = parse_source_requirements(SOURCE)
        lock = parse_dependency_lock(rendered_lock(), source_text=SOURCE)

        self.assertEqual(
            [(pin.name, pin.normalized_name, pin.version, pin.marker) for pin in pins],
            [
                ("Alpha_Pkg", "alpha-pkg", "1.2.3", None),
                ("colorama", "colorama", "0.4.6", 'platform_system == "Windows"'),
                (
                    "typing_extensions",
                    "typing-extensions",
                    "4.16.0",
                    'python_version < "3.11"',
                ),
            ],
        )
        self.assertEqual(
            [
                (item.pin.normalized_name, item.pin.version, item.pin.marker)
                for item in lock.requirements
            ],
            [(pin.normalized_name, pin.version, pin.marker) for pin in pins],
        )

        with self.assertRaises(DependencyLockError) as caught:
            parse_dependency_lock(
                lock.text,
                source_text=SOURCE.replace("Alpha_Pkg==1.2.3", "Alpha_Pkg==1.2.4"),
            )
        self.assertEqual(caught.exception.code, "dependency_lock_stale")

    def test_source_and_lock_sha256_are_over_normalized_utf8_text(self):
        lock_text = rendered_lock()
        lock = parse_dependency_lock(lock_text, source_text=SOURCE)

        self.assertEqual(
            lock.source_sha256,
            hashlib.sha256(SOURCE.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(
            lock.lock_sha256,
            hashlib.sha256(lock_text.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(lock.text, lock_text)

        crlf_source = SOURCE.replace("\n", "\r\n")
        crlf_lock = render_dependency_lock(crlf_source, artifact_mapping())
        self.assertEqual(crlf_lock, lock_text)

    def test_artifact_hashes_remain_bound_to_filenames_and_sizes(self):
        lock = parse_dependency_lock(rendered_lock(), source_text=SOURCE)
        by_name = {item.pin.normalized_name: item.artifacts for item in lock.requirements}

        self.assertEqual(
            by_name["alpha-pkg"],
            (
                artifact(
                    "alpha_pkg-1.2.3-cp311-cp311-manylinux_2_17_x86_64.whl",
                    SHA_ALPHA_LINUX,
                    202,
                ),
                artifact(
                    "alpha_pkg-1.2.3-cp311-cp311-win_amd64.whl",
                    SHA_ALPHA_WINDOWS,
                    101,
                ),
            ),
        )
        self.assertEqual(by_name["colorama"][0].size, 303)
        self.assertEqual(by_name["typing-extensions"][0].sha256, SHA_TYPING_EXTENSIONS)

    def test_missing_malformed_and_orphaned_hashes_fail_closed(self):
        valid = rendered_lock()
        malformed = {
            "missing": valid.replace(
                f"    --hash=sha256:{SHA_COLORAMA}",
                "    --hash=sha256:",
                1,
            ),
            "wrong algorithm": valid.replace(
                f"    --hash=sha256:{SHA_COLORAMA}",
                f"    --hash=sha1:{SHA_COLORAMA}",
                1,
            ),
            "non hex": valid.replace(SHA_COLORAMA, "z" * 64),
            "orphan": valid + f"    --hash=sha256:{'5' * 64}\n",
        }
        for label, content in malformed.items():
            with self.subTest(label=label), self.assertRaises(DependencyLockError) as caught:
                parse_dependency_lock(content, source_text=SOURCE)
            self.assertEqual(caught.exception.code, "dependency_lock_invalid")

    def test_renderer_rejects_missing_extra_and_invalid_artifacts(self):
        cases = {
            "missing": {k: v for k, v in artifact_mapping().items() if k != "colorama"},
            "extra": {**artifact_mapping(), "unlisted": (artifact("extra.whl", "6" * 64, 1),)},
            "empty": {**artifact_mapping(), "colorama": ()},
            "invalid hash": {
                **artifact_mapping(),
                "colorama": (artifact("colorama.whl", "invalid", 1),),
            },
            "invalid size": {
                **artifact_mapping(),
                "colorama": (artifact("colorama.whl", SHA_COLORAMA, 0),),
            },
        }
        for label, mapping in cases.items():
            with self.subTest(label=label), self.assertRaises(DependencyLockError) as caught:
                render_dependency_lock(SOURCE, mapping)
            self.assertEqual(caught.exception.code, "dependency_lock_invalid")

    def test_rendering_is_stable_across_mapping_and_artifact_order(self):
        self.assertEqual(rendered_lock(), rendered_lock(reverse=True))

    def test_load_dependency_lock_checks_the_source_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "requirements.txt"
            lock_path = root / "requirements.lock"
            source.write_text(SOURCE, encoding="utf-8", newline="\n")
            lock_path.write_text(rendered_lock(), encoding="utf-8", newline="\n")

            loaded = load_dependency_lock(lock_path, source)
            self.assertEqual(loaded.source_sha256, hashlib.sha256(SOURCE.encode()).hexdigest())

            source.write_text(
                SOURCE.replace("Alpha_Pkg==1.2.3", "Alpha_Pkg==1.2.4"),
                encoding="utf-8",
                newline="\n",
            )
            with self.assertRaises(DependencyLockError) as caught:
                load_dependency_lock(lock_path, source)
            self.assertEqual(caught.exception.code, "dependency_lock_stale")

    def test_wheelhouse_verification_binds_each_active_wheel_to_bytes(self):
        source = 'Alpha_Pkg==1.2.3\ncolorama==0.4.6; platform_system == "Windows"\n'
        alpha_bytes = b"alpha-wheel"
        colorama_bytes = b"colorama-wheel"
        mapping = {
            "alpha-pkg": (
                artifact(
                    "alpha_pkg-1.2.3-py3-none-any.whl",
                    hashlib.sha256(alpha_bytes).hexdigest(),
                    len(alpha_bytes),
                ),
            ),
            "colorama": (
                artifact(
                    "colorama-0.4.6-py2.py3-none-any.whl",
                    hashlib.sha256(colorama_bytes).hexdigest(),
                    len(colorama_bytes),
                ),
            ),
        }
        lock = parse_dependency_lock(
            render_dependency_lock(source, mapping),
            source_text=source,
        )
        with tempfile.TemporaryDirectory() as temp:
            wheelhouse = Path(temp)
            (wheelhouse / mapping["alpha-pkg"][0].filename).write_bytes(alpha_bytes)
            (wheelhouse / mapping["colorama"][0].filename).write_bytes(colorama_bytes)
            result = verify_wheelhouse(
                lock,
                wheelhouse,
                {"platform_system": "Windows", "python_version": "3.10"},
            )
            self.assertEqual(result["files"], 2)
            self.assertEqual(result["requirements"], 2)

            (wheelhouse / mapping["colorama"][0].filename).write_bytes(b"tampered")
            with self.assertRaises(DependencyLockError) as caught:
                verify_wheelhouse(
                    lock,
                    wheelhouse,
                    {"platform_system": "Windows", "python_version": "3.10"},
                )
            self.assertEqual(caught.exception.code, "dependency_wheel_hash_mismatch")


class DependencyLockGenerationTests(unittest.TestCase):
    def test_collect_artifacts_keeps_other_platform_wheels_and_excludes_sdist(self):
        pin = parse_source_requirements("Alpha_Pkg==1.2.3\n")[0]
        urls = [
            {
                "filename": "alpha_pkg-1.2.3-cp311-cp311-win_amd64.whl",
                "url": (
                    "https://files.pythonhosted.org/packages/"
                    "alpha_pkg-1.2.3-cp311-cp311-win_amd64.whl"
                ),
                "size": 101,
                "packagetype": "bdist_wheel",
                "yanked": False,
                "digests": {"sha256": SHA_ALPHA_WINDOWS},
            },
            {
                "filename": "alpha_pkg-1.2.3-cp311-cp311-manylinux_2_17_x86_64.whl",
                "url": (
                    "https://files.pythonhosted.org/packages/"
                    "alpha_pkg-1.2.3-cp311-cp311-manylinux_2_17_x86_64.whl"
                ),
                "size": 202,
                "packagetype": "bdist_wheel",
                "yanked": False,
                "digests": {"sha256": SHA_ALPHA_LINUX},
            },
            {
                "filename": "alpha_pkg-1.2.3.tar.gz",
                "url": "https://files.pythonhosted.org/packages/alpha_pkg-1.2.3.tar.gz",
                "size": 303,
                "packagetype": "sdist",
                "yanked": False,
                "digests": {"sha256": "5" * 64},
            },
            {
                "filename": "alpha_pkg-1.2.3-yanked.whl",
                "url": "https://files.pythonhosted.org/packages/alpha_pkg-1.2.3-yanked.whl",
                "size": 404,
                "packagetype": "bdist_wheel",
                "yanked": True,
                "digests": {"sha256": "6" * 64},
            },
        ]
        verified = []

        def fetch(requested):
            self.assertEqual(requested, pin)
            return {
                "info": {"name": "Alpha-Pkg", "version": "1.2.3"},
                "urls": list(reversed(urls)),
            }

        def verify(url, locked):
            verified.append((url, locked))

        collected = updater.collect_artifacts(
            (pin,),
            metadata_fetcher=fetch,
            artifact_verifier=verify,
        )

        self.assertEqual(set(collected), {"alpha-pkg"})
        self.assertEqual(
            {item.filename for item in collected["alpha-pkg"]},
            {
                "alpha_pkg-1.2.3-cp311-cp311-win_amd64.whl",
                "alpha_pkg-1.2.3-cp311-cp311-manylinux_2_17_x86_64.whl",
            },
        )
        self.assertEqual(
            {item.size for item in collected["alpha-pkg"]},
            {101, 202},
        )
        self.assertEqual(
            {url for url, _locked in verified},
            {
                (
                    "https://files.pythonhosted.org/packages/"
                    "alpha_pkg-1.2.3-cp311-cp311-win_amd64.whl"
                ),
                (
                    "https://files.pythonhosted.org/packages/"
                    "alpha_pkg-1.2.3-cp311-cp311-manylinux_2_17_x86_64.whl"
                ),
            },
        )

    def test_collect_artifacts_is_stable_when_index_order_changes(self):
        pins = parse_source_requirements("Alpha_Pkg==1.2.3\n")
        entries = [
            {
                "filename": "alpha_pkg-1.2.3-py3-none-any.whl",
                "url": (
                    "https://files.pythonhosted.org/packages/"
                    "alpha_pkg-1.2.3-py3-none-any.whl"
                ),
                "size": 10,
                "packagetype": "bdist_wheel",
                "yanked": False,
                "digests": {"sha256": SHA_ALPHA_WINDOWS},
            },
            {
                "filename": "alpha_pkg-1.2.3.tar.gz",
                "url": "https://files.pythonhosted.org/packages/alpha_pkg-1.2.3.tar.gz",
                "size": 20,
                "packagetype": "sdist",
                "yanked": False,
                "digests": {"sha256": SHA_ALPHA_LINUX},
            },
        ]

        def collect(urls):
            return updater.collect_artifacts(
                pins,
                metadata_fetcher=lambda _pin: {
                    "info": {"name": "Alpha_Pkg", "version": "1.2.3"},
                    "urls": urls,
                },
                artifact_verifier=lambda _url, _artifact: None,
            )

        forward = collect(entries)
        reverse = collect(list(reversed(entries)))
        self.assertEqual(forward, reverse)
        self.assertEqual(
            render_dependency_lock("Alpha_Pkg==1.2.3\n", forward),
            render_dependency_lock("Alpha_Pkg==1.2.3\n", reverse),
        )

    def test_universal_wheel_supports_every_declared_target(self):
        filename = "universal_pkg-1.0.0-py3-none-any.whl"

        self.assertEqual(len(SUPPORTED_DEPENDENCY_TARGETS), 6)
        self.assertEqual(
            {
                (target.platform_system, target.python_version)
                for target in SUPPORTED_DEPENDENCY_TARGETS
            },
            {
                (system, version)
                for system in ("Windows", "Linux")
                for version in ("3.10", "3.11", "3.12")
            },
        )
        self.assertTrue(
            all(
                wheel_supports_target(filename, target)
                for target in SUPPORTED_DEPENDENCY_TARGETS
            )
        )

    def test_exact_cpython_wheels_are_platform_and_minor_specific(self):
        windows = "exact_pkg-1.0.0-cp311-cp311-win_amd64.whl"
        linux = "exact_pkg-1.0.0-cp311-cp311-manylinux_2_17_x86_64.whl"
        targets = {
            (target.platform_system, target.python_version): target
            for target in SUPPORTED_DEPENDENCY_TARGETS
        }

        self.assertTrue(wheel_supports_target(windows, targets[("Windows", "3.11")]))
        self.assertFalse(wheel_supports_target(windows, targets[("Linux", "3.11")]))
        self.assertFalse(wheel_supports_target(windows, targets[("Windows", "3.10")]))
        self.assertFalse(wheel_supports_target(windows, targets[("Windows", "3.12")]))
        self.assertTrue(wheel_supports_target(linux, targets[("Linux", "3.11")]))
        self.assertFalse(wheel_supports_target(linux, targets[("Windows", "3.11")]))

    def test_cp39_abi3_wheels_cover_all_supported_python_minors(self):
        windows = "abi_pkg-1.0.0-cp39-abi3-win_amd64.whl"
        linux = "abi_pkg-1.0.0-cp39-abi3-manylinux_2_17_x86_64.whl"

        for target in SUPPORTED_DEPENDENCY_TARGETS:
            with self.subTest(target=target.identifier):
                expected = windows if target.platform_system == "Windows" else linux
                other = linux if target.platform_system == "Windows" else windows
                self.assertTrue(wheel_supports_target(expected, target))
                self.assertFalse(wheel_supports_target(other, target))

    def test_target_coverage_honors_markers_and_accepts_complete_wheel_sets(self):
        source = (
            "UniversalPkg==1.0.0\n"
            "ExactPkg==1.0.0\n"
            'WindowsOnly==1.0.0; platform_system == "Windows"\n'
        )
        exact_wheels = []
        for index, target in enumerate(SUPPORTED_DEPENDENCY_TARGETS, 10):
            python_tag = f"cp{target.python_version.replace('.', '')}"
            platform_tag = (
                "win_amd64"
                if target.platform_system == "Windows"
                else "manylinux_2_17_x86_64"
            )
            exact_wheels.append(
                artifact(
                    f"exact_pkg-1.0.0-{python_tag}-{python_tag}-{platform_tag}.whl",
                    hashlib.sha256(target.identifier.encode("utf-8")).hexdigest(),
                    index,
                )
            )
        mapping = {
            "universalpkg": (
                artifact("universal_pkg-1.0.0-py3-none-any.whl", "a" * 64, 1),
            ),
            "exactpkg": tuple(exact_wheels),
            "windowsonly": (
                artifact("windows_only-1.0.0-py3-none-win_amd64.whl", "b" * 64, 2),
            ),
        }
        lock = parse_dependency_lock(
            render_dependency_lock(source, mapping),
            source_text=source,
        )

        coverage = validate_target_coverage(lock)

        self.assertEqual(set(coverage), {target.identifier for target in SUPPORTED_DEPENDENCY_TARGETS})
        for target in SUPPORTED_DEPENDENCY_TARGETS:
            with self.subTest(target=target.identifier):
                self.assertIn("universalpkg", coverage[target.identifier])
                self.assertIn("exactpkg", coverage[target.identifier])
                if target.platform_system == "Windows":
                    self.assertIn("windowsonly", coverage[target.identifier])
                else:
                    self.assertNotIn("windowsonly", coverage[target.identifier])

    def test_missing_one_target_wheel_fails_closed(self):
        source = "ExactPkg==1.0.0\n"
        artifacts = []
        missing = next(
            target
            for target in SUPPORTED_DEPENDENCY_TARGETS
            if target.platform_system == "Linux" and target.python_version == "3.12"
        )
        for index, target in enumerate(SUPPORTED_DEPENDENCY_TARGETS, 20):
            if target == missing:
                continue
            python_tag = f"cp{target.python_version.replace('.', '')}"
            platform_tag = (
                "win_amd64"
                if target.platform_system == "Windows"
                else "manylinux_2_17_x86_64"
            )
            artifacts.append(
                artifact(
                    f"exact_pkg-1.0.0-{python_tag}-{python_tag}-{platform_tag}.whl",
                    hashlib.sha256(target.identifier.encode("utf-8")).hexdigest(),
                    index,
                )
            )
        lock = parse_dependency_lock(
            render_dependency_lock(source, {"exactpkg": artifacts}),
            source_text=source,
        )

        with self.assertRaises(DependencyLockError) as caught:
            validate_target_coverage(lock)

        self.assertEqual(caught.exception.code, "dependency_lock_target_incomplete")
        self.assertIn(missing.identifier, str(caught.exception))

    def test_collect_artifacts_rejects_invalid_identity_hash_and_size(self):
        pin = parse_source_requirements("Alpha_Pkg==1.2.3\n")[0]
        base = {
            "info": {"name": "Alpha-Pkg", "version": "1.2.3"},
            "urls": [{
                "filename": "alpha_pkg-1.2.3-py3-none-any.whl",
                "url": (
                    "https://files.pythonhosted.org/packages/"
                    "alpha_pkg-1.2.3-py3-none-any.whl"
                ),
                "size": 10,
                "packagetype": "bdist_wheel",
                "yanked": False,
                "digests": {"sha256": SHA_ALPHA_WINDOWS},
            }],
        }
        bad_payloads = {
            "name": {**base, "info": {"name": "Other", "version": "1.2.3"}},
            "version": {**base, "info": {"name": "Alpha-Pkg", "version": "9.9.9"}},
            "hash": {
                **base,
                "urls": [{**base["urls"][0], "digests": {"sha256": "bad"}}],
            },
            "size": {**base, "urls": [{**base["urls"][0], "size": 0}]},
        }
        for label, payload in bad_payloads.items():
            with (
                self.subTest(label=label),
                self.assertRaises(DependencyLockError),
            ):
                updater.collect_artifacts(
                    (pin,),
                    metadata_fetcher=lambda _pin, payload=payload: payload,
                    artifact_verifier=lambda _url, _artifact: None,
                )

    def test_describe_changes_is_stable_and_reports_artifact_identity(self):
        old_lock = parse_dependency_lock(rendered_lock(), source_text=SOURCE)
        changed_mapping = artifact_mapping()
        changed_mapping["colorama"] = (
            artifact("colorama-0.4.6-py2.py3-none-any.whl", "7" * 64, 999),
        )
        new_lock = parse_dependency_lock(
            render_dependency_lock(SOURCE, changed_mapping),
            source_text=SOURCE,
        )

        first = updater.describe_changes(old_lock, new_lock)
        second = updater.describe_changes(old_lock, new_lock)
        self.assertEqual(first, second)
        self.assertIn("colorama", repr(first).lower())
        self.assertIn(SHA_COLORAMA, repr(first))
        self.assertIn("7" * 64, repr(first))

    def test_atomic_replace_failure_preserves_old_lock_and_cleans_temporary(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "requirements.lock"
            target.write_text("old lock\n", encoding="utf-8", newline="\n")

            with (
                patch.object(updater.os, "replace", side_effect=OSError("replace failed")),
                self.assertRaises(OSError),
            ):
                updater.atomic_write(target, "new lock\n")

            self.assertEqual(target.read_text(encoding="utf-8"), "old lock\n")
            self.assertEqual(list(target.parent.glob(".requirements.lock.*.tmp")), [])

    def test_update_download_failure_preserves_the_previous_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "requirements.txt"
            target = root / "requirements.lock"
            source.write_text(SOURCE, encoding="utf-8", newline="\n")
            old = rendered_lock()
            target.write_text(old, encoding="utf-8", newline="\n")

            with (
                patch.object(
                    updater,
                    "collect_artifacts",
                    side_effect=DependencyLockError(
                        "dependency_lock_update_failed",
                        "download failed",
                    ),
                ),
                self.assertRaises(DependencyLockError),
            ):
                updater.update_lock(
                    source_path=source,
                    lock_path=target,
                    apply=True,
                )

            self.assertEqual(target.read_text(encoding="utf-8"), old)
            self.assertEqual(list(root.glob(".requirements.lock.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
