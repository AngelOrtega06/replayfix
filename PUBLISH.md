# Put the project on GitHub

This package is prepared for upload. No GitHub repository, release, video, or
hackathon submission has been created by these files.

## 1. Extract the archive

Follow `SETUP.md`, or use File Explorer's **Extract All** on
`ReplayFix_GitHub_Ready.zip`. Open the extracted folder so that `README.md`,
`LICENSE`, and `replayfix.py` are visible alongside the `fixtures`, `tests`,
and `examples` folders.

## 2. Create the repository

Open https://github.com/new and use:

| Field | Value |
| --- | --- |
| Repository name | `replayfix` |
| Description | AI repair agent using NVIDIA Nemotron on Nebius Token Factory, with Docker test verification and an evidence dashboard. |
| Visibility | Public |
| Initialize files | Leave the README, ignore-file, and license initialization options off; these files are already included |

Create the repository, then open its file-upload page. Upload the **contents**
of the extracted folder, including its subfolders, so `README.md` appears at
the repository root. Commit the files with a message such as
`Add ReplayFix demo and verified run evidence`.

Use the extracted source files for this upload. The ZIP can be attached to a
release separately if a downloadable test build is needed.

GitHub guidance:
- https://docs.github.com/en/repositories/creating-and-managing-repositories/creating-a-new-repository
- https://docs.github.com/en/repositories/working-with-files/managing-files/adding-a-file-to-a-repository

## 3. Check the uploaded project

Confirm that the README renders, the MIT license is detected, and the three
evidence files are present under `examples/20261013T221547Z_45f0a4/`.
The baseline fixture must remain buggy: it is the input to the repair agent.
The repaired handler belongs in the evidence directory.

## Remaining submission work

The event requires a public source repository with an open-source license and
setup instructions, a working demo/application/test-build URL, a public YouTube
demo under three minutes, a project description and selected track, and tool
feedback. The best fit for this project is **Coding and Agentic Engineering**.

The two-minute script is in `DEMO_SCRIPT.md`. Publishing the repository is one
step; the demonstration URL, video, and Devpost submission still need to be
prepared. Arrange access that lets judges test the working project without a
charge. The saved example supports inspection, while new repairs require live
Token Factory access. The local `127.0.0.1` address is not a remotely accessible
demo link.

Official requirements, checked October 13, 2026:
https://nebiusglobalaihackathon.devpost.com/rules
