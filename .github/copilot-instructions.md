# Instructions for Copilot

## Branch and PR workflow

For code, test, or documentation changes in this repository, prefer creating a
dedicated fix or feature branch from `malkam03-dev` and opening a pull request
back into `malkam03-dev`. Keep unrelated dirty files unstaged and out of the PR.
Only commit directly to `malkam03-dev` when the user explicitly requests direct
branch edits.

Important: remember to add unit tests for any new functionality you add.
This is mission critical code, so we need to ensure that it works as expected and doesn't break anything.

You can find the tests in the `tests` directory and be compatible with pytest.
You can run the tests with `pytest` and check the coverage with `pytest --cov`.

We need a high level of test coverage, so please make sure to add tests for any new functionality you add. Additionally, ensure that all tests are well-documented and follow best practices.