# Hugging Face 배치 자동화 런북

이 런북은 워커에 통합된 승인된 자동화 경로를 설명합니다. 수집된 ROS2
데이터를 staging하고, `third_party/rosbag-to-lerobot`으로 LeRobot dataset으로
변환한 뒤, Hugging Face에 업로드하고 remote verify가 끝난 후에만 로컬
데이터를 정리합니다.

## 패키지 진입점

- `aic-collector-worker`를 사용할 수 있도록 프로젝트를 정상 설치합니다.
- `aic-collector-worker --hf-repo-id ...`가 수집, 변환, Hugging Face 업로드,
  remote verification, cleanup gate를 하나의 워커 실행 경로에서 담당합니다.
- `aic-automation-batch`는 upload flag를 붙여 워커에 위임하는 얇은 wrapper로
  유지합니다. 별도의 경쟁 upload path를 구현하면 안 됩니다.
- Streamlit UI는 일반 워커 컨트롤을 통해 이 기능을 노출합니다. 워커의
  `limit`은 전체 episode 수이고, `upload_batch_size`는
  수집/변환/업로드/정리 묶음 크기입니다. 별도 "Batch → LeRobot → Hugging Face"
  패널을 추가하지 않습니다.
- Hugging Face 업로드 런타임 의존성은 `huggingface_hub`입니다.

## Hugging Face 인증

Streamlit UI 밖에서 표준 Hugging Face 인증을 사용합니다. 사용자는 워커를
시작하기 전에 아래 두 값만 준비하면 됩니다.

- `HF_TOKEN`: dataset repo에 쓰기 권한이 있는 Hugging Face access token
- `--hf-repo-id`: dataset repo 이름. 예: `org_or_user/aic-round-1800`

처음 설정하는 운영자용 빠른 절차:

```bash
# 1) 새 환경이면 프로젝트 의존성을 설치합니다.
uv sync

# 2) Hugging Face 웹 UI에서 dataset repo를 만들거나 기존 repo를 선택합니다.
#    repo id를 org_or_user/dataset 형태 그대로 복사합니다.
export AIC_HF_REPO_ID=org_or_user/dataset

# 3) 셸에서 인증합니다. 토큰을 config 파일에 넣지 않습니다.
export HF_TOKEN=hf_...

# 4) 긴 수집을 시작하기 전에 인증과 repo 접근이 되는지 확인합니다.
uv run python - <<'PY'
import os
from huggingface_hub import HfApi

repo_id = os.environ["AIC_HF_REPO_ID"]
files = HfApi().list_repo_files(repo_id=repo_id, repo_type="dataset")
print(f"HF 접근 확인 완료: {repo_id} ({len(files)}개 파일 확인)")
PY

# 5) 워커에는 repo id만 넘깁니다. 인증 정보는 환경변수에서 읽습니다.
uv run aic-collector-worker --root configs/train --task all \
    --limit 20 --upload-batch-size 5 \
    --policy cheatcode --collect-episode true \
    --hf-repo-id "$AIC_HF_REPO_ID" \
    --converter-path third_party/rosbag-to-lerobot
```

대체 인증 방법:

1. 셸과 CI 권장 방식:
   ```bash
   export HF_TOKEN=hf_...
   ```
2. 운영자 워크스테이션 권장 방식:
   ```bash
   huggingface-cli login
   ```

HF token을 UI, dataset 입력 필드, manifest, 로그, config 파일에 붙여넣지
마세요. UI는 token을 저장하면 안 됩니다. 운영자는 `HF_TOKEN` 또는 로컬
Hugging Face login cache로만 인증 정보를 제공해야 합니다.

## Converter submodule/path 설정

converter는 `third_party/rosbag-to-lerobot` 경로에 있어야 합니다.

```bash
git submodule update --init --recursive third_party/rosbag-to-lerobot
```

실행 전에 경로가 존재하고 converter의 문서화된 entry point/config 형식을 사용할
수 있는지 확인합니다. converter 입력은 원본 MCAP/rosbag artifact를 batch 전용
staging directory로 복사, hardlink, symlink해서 비파괴적으로 준비합니다. 변환
중 원본 run directory를 이동하거나 삭제하지 않습니다.

## 상태와 복구

append-only batch manifest가 source of truth입니다. 정상 lifecycle은 아래와
같습니다.

`planned -> worker_started -> worker_finished -> reconciled -> collected_validated -> staged -> converted -> uploaded -> remote_verified -> cleanup_eligible -> cleanup_done`

복구 규칙:

- 실행이 `uploaded` 전에 멈추면 마지막으로 성공한 로컬 상태에서 재개합니다.
  기록된 digest가 여전히 맞으면 기존 staged/converted path를 재사용합니다.
- 실행이 `uploaded`에서 멈추면 기록된 repo id, dataset repo type,
  commit/revision, upload timestamp, local digest, file count를 사용해 remote
  verification부터 재개합니다. 중복 batch를 무조건 다시 업로드하지 않습니다.
- remote verification이 실패하면 로컬 artifact를 그대로 보존하고 운영자 확인 후
  verification 또는 upload를 재시도합니다.
- `cleanup_eligible` 이후 cleanup이 실패하면 manifest에 기록된 path만 다시
  cleanup하고 새 result event를 append합니다.

## Cleanup 안전성

정확한 batch와 uploaded revision에 대한 `remote_verified` 증거가 있을 때만
cleanup합니다. `remote_verified` 전에 수집 데이터를 삭제하면 안 됩니다. 워커는
검증된 upload batch마다 raw run directory, staging folder, 임시 LeRobot
folder를 삭제할 수 있지만, 해당 batch manifest에 명시된 path만 삭제해야
합니다. 이후 삭제된 path와 timestamp를 tombstone으로 기록한 `cleanup_done`을
append합니다.

manifest, log, 관련 없는 queue root는 절대 삭제하지 않습니다. 승인된 MVP에는
unchecked destructive cleanup mode가 없습니다.

## 보안 검증 결과

이 워커 영역의 static review 결과, Hugging Face credential을 저장하는 구현
코드는 발견되지 않았습니다. package/docs check는 인증이 `HF_TOKEN` 또는
`huggingface-cli login`으로 유지되고, 운영자 문서가 UI token 저장을 명시적으로
금지하도록 강제합니다.

static review는 문서화된 cleanup gate도 확인합니다. cleanup은
`remote_verified` 이후에만 진행되며, 이후 `cleanup_eligible`과 `cleanup_done`이
기록됩니다. `remote_verified` 전에 cleanup을 추가하는 구현 변경은 거절하거나,
merge 전에 실패하는 regression test로 먼저 보호해야 합니다.
