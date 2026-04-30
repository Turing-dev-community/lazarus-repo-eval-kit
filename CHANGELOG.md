# Changelog

## [0.3.0](https://github.com/Turing-dev-community/lazarus-repo-eval-kit/compare/v0.2.0...v0.3.0) (2026-04-30)


### Features

* Add enterprise signals framework ([bfea4cb](https://github.com/Turing-dev-community/lazarus-repo-eval-kit/commit/bfea4cb712d7e9c403f80648ca66daef160708f0))
* Add hybrid enterprise-scale data handling collector (E17) ([a6b3e47](https://github.com/Turing-dev-community/lazarus-repo-eval-kit/commit/a6b3e477da825f774ee4fc849f4906fd10035b5b))
* Add LLM-based broken evaluator risk collector (E10) ([6c14c9c](https://github.com/Turing-dev-community/lazarus-repo-eval-kit/commit/6c14c9cc226dd58619912a4e903222312e4325fb))
* Add LLM-based enterprise domain complexity collector (E2) ([c1b18bb](https://github.com/Turing-dev-community/lazarus-repo-eval-kit/commit/c1b18bbe446ed5a5f3eafdea6d09cc4b47fc4682))
* Add LLM-based multi-tenancy & permission logic collector (E6) ([3c0ba5d](https://github.com/Turing-dev-community/lazarus-repo-eval-kit/commit/3c0ba5d1e3e0630208802fb3bbf19a2dc7205e8d))
* Add LLM-based production incident signal collector ([27ca3da](https://github.com/Turing-dev-community/lazarus-repo-eval-kit/commit/27ca3da69a504f705c099d4348737184b30a17c5))
* Add LLM-based vendor integration / adapter shims collector (E12) ([f0d50e8](https://github.com/Turing-dev-community/lazarus-repo-eval-kit/commit/f0d50e8971d524e736f6c5815c41da042918c66f))
* Add programmatic adjacent artifacts collector (E7) ([c5b1f39](https://github.com/Turing-dev-community/lazarus-repo-eval-kit/commit/c5b1f39058b0631ecba72cc46fe6f417109332ad))
* Add programmatic CI/CD guardrails collector (E13) ([9b3f12e](https://github.com/Turing-dev-community/lazarus-repo-eval-kit/commit/9b3f12ed48b89ca997166b5dee35c502a1a08557))
* Add programmatic cross-service boundary collector (E3) ([487d7ea](https://github.com/Turing-dev-community/lazarus-repo-eval-kit/commit/487d7eaf0346497b11645860436b6310dbfa4e0d))
* Add programmatic DB migration PR detector (E5) ([8b70939](https://github.com/Turing-dev-community/lazarus-repo-eval-kit/commit/8b70939ad9dec53308176d6b37ee6634e0b90baf))
* Add programmatic dependency list collector (E4) ([92f3d65](https://github.com/Turing-dev-community/lazarus-repo-eval-kit/commit/92f3d658f5855aa97608e56306aaf8f7bd578211))
* Add programmatic environment sensitivity collector (E9) ([82e1c2e](https://github.com/Turing-dev-community/lazarus-repo-eval-kit/commit/82e1c2e492e743d51213ea3ef1443ec16baa7db4))
* Add programmatic feature flagging collector (E15) ([0742c61](https://github.com/Turing-dev-community/lazarus-repo-eval-kit/commit/0742c614be053ee0d3006ce1c9bb2fb2ea52b5b5))
* Add programmatic hardware/environment gaps collector (E11) ([62166c2](https://github.com/Turing-dev-community/lazarus-repo-eval-kit/commit/62166c272e55bd9116213d10b97bf0ba77ea6e1f))
* Add programmatic monorepo cross-package PR collector (E8) ([8faa74c](https://github.com/Turing-dev-community/lazarus-repo-eval-kit/commit/8faa74cb998e808523a9c8706edc19c002206d5b))
* Add programmatic PR description quality scorer (E14) ([1101e1c](https://github.com/Turing-dev-community/lazarus-repo-eval-kit/commit/1101e1ceacb63a3f3d8a85830db22987c0c49ea7))
* Add programmatic resiliency patterns collector (E16) ([ae21651](https://github.com/Turing-dev-community/lazarus-repo-eval-kit/commit/ae2165185b2e46a9b23904ec9793a50271b41ac5))
* Expose per-PR enterprise signals as top-level pr_enterprise_signals ([ba659d8](https://github.com/Turing-dev-community/lazarus-repo-eval-kit/commit/ba659d8028ab8e831f939081064dcdfc7075e8fd))
* Wire all enterprise signal collectors into repo_evaluator (E3-E17) ([c94b587](https://github.com/Turing-dev-community/lazarus-repo-eval-kit/commit/c94b5870eb12638071927b9240abf28fad7f25a3))


### Bug Fixes

* Filter same-repo GitHub links in adjacent artifacts ([d024fbd](https://github.com/Turing-dev-community/lazarus-repo-eval-kit/commit/d024fbdcfb05a7c65111b28b5809847c23e8db71))
* Remove enterprise_signals from pass_first_filter_prs per-PR records ([378b6c0](https://github.com/Turing-dev-community/lazarus-repo-eval-kit/commit/378b6c0a7b20580bd9fa686104b497f3b1419f84))

## [0.2.0](https://github.com/Turing-dev-community/lazarus-repo-eval-kit/compare/v0.1.0...v0.2.0) (2026-04-28)


### Features

* Add fairness evaluator for F2P tests ([8f37e44](https://github.com/Turing-dev-community/lazarus-repo-eval-kit/commit/8f37e4400e2ab7a19839214db9d1a2a26b285916))
* Add LLM cost tracking and warnings ([9948589](https://github.com/Turing-dev-community/lazarus-repo-eval-kit/commit/9948589ada85217dd9bf849dabff340bb0cab53d))
* Update JVM runtime checks and document prerequisites ([ecad57e](https://github.com/Turing-dev-community/lazarus-repo-eval-kit/commit/ecad57e983e7435ad8deaa71d9fbeaa3ae8350e6))


### Bug Fixes

* Ensure thread-safety for UsageTracker ([f23f921](https://github.com/Turing-dev-community/lazarus-repo-eval-kit/commit/f23f9219ce39885a0e7a409570095d3cff86a9b0))
* Move cost limit abort handling to main function to preserve ([1c33238](https://github.com/Turing-dev-community/lazarus-repo-eval-kit/commit/1c332383735394bbf1dd45543dfe3cb2f7032c10))
* Optimize rubric accepted count calculation ([53d7272](https://github.com/Turing-dev-community/lazarus-repo-eval-kit/commit/53d72729eee39f7af597e3ac3c13fc565d1b7b12))
* Remove test name extraction from fairness evaluator ([e75e9ff](https://github.com/Turing-dev-community/lazarus-repo-eval-kit/commit/e75e9ff8abc76e690ceb412a501ef921a8ee20e0))
* use the correct version range for genai-prices ([33ccea4](https://github.com/Turing-dev-community/lazarus-repo-eval-kit/commit/33ccea48c0d29c22938e52f913b365f5a7a324dc))
