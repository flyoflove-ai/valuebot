#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
valuebot.py  v1.0
애널리스트 보고서 -> 목표주가 산정 근거/계산식 역산 + 파이썬 검산 봇

설계 원칙
  1) LLM은 "추출"만 시키고, "산수"는 파이썬이 한다.
     (LLM 곱셈은 신뢰 불가 -> EPS x Multiple 검산은 코드로)
  2) 상주 롱폴링 루프 (cron 의존 X)
  3) Gemini 모델 런타임 디스커버리 + 폴백 체인
  4) 429 fail-fast + offset 선진행 (같은 메시지 무한 재시도 방지)

ENV
  VALUEBOT_TG_TOKEN    (필수) BotFather 토큰
  VALUEBOT_GEMINI_KEY  (필수) 이 봇 전용 Gemini 키
  VALUEBOT_ALLOWED     (선택) 허용 chat_id 콤마구분. 미설정 시 전체 허용
  VALUEBOT_MODEL       (선택) 모델 강제 지정
  VALUEBOT_MAX_RUNTIME (선택) 초. 기본 19800(5.5h)
"""

import os
import re
import sys
import json
import time
import base64
import traceback
import requests

# ----------------------------------------------------------------------------
# 설정
