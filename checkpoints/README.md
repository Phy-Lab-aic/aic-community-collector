# checkpoints/

모델 체크포인트를 보관하는 디렉토리입니다.

## pi05_aic_cable_insert_ur5e

OpenPI (pi05) AIC cable insertion 모델.

| 항목 | 값 |
|---|---|
| HuggingFace 저장소 | `Phy-lab/aic_cable_insert_sft_openpi_pi05_pretrained_v3_bs128_mb16_step11040` |
| 기본 체크포인트 | `global_step_11040` |
| 실제 가중치 파일 | `actor/model_state_dict/full_weights.pt` (~8GB) |

### 로컬 심볼릭 링크 설정

HuggingFace 캐시가 이미 다운로드된 경우 심볼릭 링크로 연결합니다:

```bash
mkdir -p checkpoints/pi05_aic_cable_insert_ur5e
ln -sfn ~/.cache/huggingface/hub/models--Phy-lab--aic_cable_insert_sft_openpi_pi05_pretrained_v3_bs128_mb16_step11040/snapshots/<HASH>/global_step_11040 \
    checkpoints/pi05_aic_cable_insert_ur5e/global_step_11040
```

심볼릭 링크가 없거나 깨진 경우 `RunOpenPI` policy가 자동으로 HuggingFace에서 다운로드합니다.
