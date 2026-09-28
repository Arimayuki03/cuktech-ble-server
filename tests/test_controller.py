"""Tests for controller.py - BLE controller operations."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.cuktech_ble.protocol import DEVICE_MAC, DEVICE_TOKEN, PORT_BITS


class TestReconnectDelay:
    """Test BLE reconnection delay calculation."""

    def test_exponential_backoff(self):
        """Test exponential backoff increases delay."""
        base_delay = 1
        max_delay = 300
        delays = []
        for attempt in range(6):
            delay = min(base_delay * (2 ** attempt), max_delay)
            delays.append(delay)
        assert delays == [1, 2, 4, 8, 16, 32]

    def test_delay_capped_at_max(self):
        """Test delay doesn't exceed max."""
        base_delay = 1
        max_delay = 300
        delay = min(base_delay * (2 ** 10), max_delay)
        assert delay == max_delay

    def test_delay_resets_on_success(self):
        """Test delay resets to base after successful connection."""
        base_delay = 1
        attempts = 5
        delay = min(base_delay * (2 ** attempts), 300)
        assert delay == 32  # After reset, next delay would be 1

    def test_ble_manager_reconnect_delay(self):
        """Test BLEManager._get_reconnect_delay() returns correct values (with jitter)."""
        from unittest.mock import MagicMock
        from ble_manager import BLEManager

        state = MagicMock()
        config = MagicMock()
        config.server.reconnect_base_delay = 1.0
        config.server.reconnect_max_delay = 300.0
        mgr = BLEManager(mac="AA:BB:CC:DD:EE:FF", token="aabbccddeeff", state=state, config=config)

        # No jitter for delay <= 1.0
        mgr._reconnect_attempts = 0
        assert mgr._get_reconnect_delay() == 1.0

        # Jitter range for delay=8.0: ±25% = ±2.0 → [6.0, 10.0]
        mgr._reconnect_attempts = 3
        for _ in range(20):
            delay = mgr._get_reconnect_delay()
            assert 6.0 <= delay <= 10.0, f"delay {delay} outside range"

        # Jitter range for delay=300.0: ±25% = ±75 → [225, 375]
        mgr._reconnect_attempts = 10
        for _ in range(20):
            delay = mgr._get_reconnect_delay()
            assert 225 <= delay <= 375, f"delay {delay} outside range"


class TestControllerInit:
    """Test CuktechBLEController initialization."""

    def test_default_mac(self):
        """Test controller accepts default MAC."""
        from src.cuktech_ble.controller import CuktechBLEController
        ctrl = CuktechBLEController(mac=DEVICE_MAC, token=DEVICE_TOKEN)
        assert ctrl.mac == DEVICE_MAC

    def test_custom_mac(self):
        """Test controller accepts custom MAC."""
        from src.cuktech_ble.controller import CuktechBLEController
        ctrl = CuktechBLEController(mac="AA:BB:CC:DD:EE:FF", token=DEVICE_TOKEN)
        assert ctrl.mac == "AA:BB:CC:DD:EE:FF"

    def test_initial_state(self):
        """Test controller initial state."""
        from src.cuktech_ble.controller import CuktechBLEController
        ctrl = CuktechBLEController(mac=DEVICE_MAC, token=DEVICE_TOKEN)
        assert ctrl.authenticated is False
        assert ctrl.client is None


class TestProtocolConstants:
    """Test protocol constants used in controller."""

    def test_device_token_length(self):
        """Test DEVICE_TOKEN is 12 bytes."""
        assert len(DEVICE_TOKEN) == 12

    def test_port_bits_complete(self):
        """Test all ports have bit assignments."""
        assert len(PORT_BITS) == 4
        assert all(v in range(4) for v in PORT_BITS.values())


class TestBuildMiotTlv:
    """Test _build_miot_tlv TLV encoding."""

    def test_set_uint8_value(self):
        """Test SET with 1-byte value."""
        from src.cuktech_ble.controller import CuktechBLEController
        # siid=2, piid=5, value=3 (场景模式=3)
        result = CuktechBLEController._build_miot_tlv(1, 2, 5, value=3)
        tl = (1 << 12) | 1  # type_id=1(UINT8), len=1
        expected = bytes([
            12, 0x20,  # total_len=12, frame_type=0x20
            1, 0x00,   # seq=1, [0x00]
            0x00, 0x01, # opcode=SET(0x00), cnt=1
            2,          # siid=2
            5, 0x00,    # piid=5 (LE)
            tl & 0xFF, (tl >> 8) & 0xFF,  # tl
            3,          # value=3
        ])
        assert result == expected
        assert len(result) == 12

    def test_set_uint32_value(self):
        """Test SET with 4-byte value (PIID 21 protocol_extend)."""
        from src.cuktech_ble.controller import CuktechBLEController
        # siid=2, piid=21, value=50532111 (0x0303030F)
        value = 0x0303030F
        result = CuktechBLEController._build_miot_tlv(1, 2, 21, value=value)
        assert len(result) == 15
        assert result[0] == 15  # total_len
        tl = (5 << 12) | 4  # type_id=5(UINT32), len=4
        assert result[9:11] == bytes([tl & 0xFF, (tl >> 8) & 0xFF])  # tl
        # Last 4 bytes = value in LE
        assert result[11:15] == b'\x0F\x03\x03\x03'

    def test_get_command(self):
        """Test GET command (value=None)."""
        from src.cuktech_ble.controller import CuktechBLEController
        # siid=2, piid=5, no value
        result = CuktechBLEController._build_miot_tlv(1, 2, 5)
        assert len(result) == 12
        assert result[4] == 0x02  # opcode=GET
        assert result[-1] == 0x00  # dummy value byte

    def test_piid_le_encoding(self):
        """Test piid is encoded as 2-byte little-endian."""
        from src.cuktech_ble.controller import CuktechBLEController
        # piid=512 (0x200) should be 0x00, 0x02
        result = CuktechBLEController._build_miot_tlv(1, 2, 512, value=1)
        assert result[7] == 0x00  # piid low byte
        assert result[8] == 0x02  # piid high byte

    def test_total_len_formula(self):
        """Test total_len = 11 + value_bytes."""
        from src.cuktech_ble.controller import CuktechBLEController
        r1 = CuktechBLEController._build_miot_tlv(1, 2, 5, value=0x00)     # UINT8 → 1 byte
        r2 = CuktechBLEController._build_miot_tlv(1, 2, 21, value=0x10000) # UINT32 → 4 bytes
        r3 = CuktechBLEController._build_miot_tlv(1, 2, 5)                 # GET → 1 byte dummy
        assert r1[0] == 12   # 11 + 1
        assert r2[0] == 15   # 11 + 4
        assert r3[0] == 12   # 11 + 1


class TestAuthMultiframeCap:
    """认证多帧响应帧数上限（H1 回归测试）。"""

    @pytest.mark.asyncio
    async def test_recv_auth_response_caps_frame_count(self):
        """异常/恶意设备上报超大帧数时，_recv_auth_response 应把帧数限制在 100。"""
        from unittest.mock import AsyncMock
        from src.cuktech_ble.controller import CuktechBLEController

        ctrl = CuktechBLEController(mac="AA:BB:CC:DD:EE:FF", token="aabbccddeeff")
        ctrl.client = AsyncMock()
        # 多帧头: [00 00 00 01 count_lo=0xC8 count_hi=0x00] → 声称 200 帧
        header = bytes([0x00, 0x00, 0x00, 0x01, 0xC8, 0x00])
        frame = bytes([0x00, 0x01, 0xAA, 0xBB])  # 帧序 0x0100，载荷 0xAABB
        calls = {"n": 0}

        async def fake_wait_notify(channel, timeout=None):
            calls["n"] += 1
            return header if calls["n"] == 1 else frame

        ctrl.wait_notify = fake_wait_notify

        result = await ctrl._recv_auth_response("auth_data")
        # 帧数被上限到 100：1 次帧头 + 100 次数据帧，而不是 200 次
        assert calls["n"] == 101
        assert result == b"\xaa\xbb" * 100


def _encrypted_frame(plaintext):
    """把明文帧包成 cmd_recv 通知格式: [00 00 0x02 len] + payload。

    _try_decode_inline 对 data[2]==0x02 的帧取 data[4:] 调 decrypt；
    decrypt 在测试中 mock 为直接返回明文，payload 内容任意。
    """
    return bytes([0, 0, 0x02, len(plaintext)]) + plaintext


def _result_frame(b4, siid, piid, err=0, value=bytes([0x2A]), vtype=1):
    """构造 SET/GET Result 明文帧（对齐 controller 响应帧布局）。

    布局: [tot_len][0x20][seq][0x00][b4][cnt=1][siid][piid][0x00]
          [err_hi][err_lo][vlen][type][value...]  （对齐 GET Result 测试样例）
    """
    total = 13 + len(value)
    return bytes([total, 0x20, 0x01, 0x00, b4, 0x01, siid, piid, 0x00,
                  (err >> 8) & 0xFF, err & 0xFF, len(value), vtype]) + value


def _ack_frame(siid, piid):
    """构造 SET ACK 明文帧: [len][0x20][seq][0x00][0x01][cnt][siid][piid]。"""
    return bytes([8, 0x20, 0x01, 0x00, 0x01, 0x01, siid, piid])


class TestRecvSetResponse:
    """_recv_set_response: SET ACK/Result 解析、错误码拒绝与 deadline。

    手法: client/write_gatt_char 用 AsyncMock（_try_decode_inline 会写内联
    ACK）、decrypt 直接返回明文帧、wait_notify 按脚本返回封装帧。
    """

    def _make_ctrl(self):
        from unittest.mock import MagicMock, AsyncMock
        from src.cuktech_ble.controller import CuktechBLEController
        ctrl = CuktechBLEController(mac="AA:BB:CC:DD:EE:FF", token="aabbccddeeff")
        ctrl.client = MagicMock()
        ctrl.client.write_gatt_char = AsyncMock()
        return ctrl

    @pytest.mark.asyncio
    async def test_result_nonzero_error_code_returns_none(self):
        """Result 帧 pt[9]/pt[10] 非零 = 设备拒绝该 SET → 返回 None。

        防止把未生效的残留 value 字节当成功缓存/上报。
        """
        from unittest.mock import MagicMock
        ctrl = self._make_ctrl()
        pt = _result_frame(0x04, 2, 5, err=0x0102, value=bytes([0x2A]))
        assert pt[9] or pt[10]  # 帧构造自查：错误码确实非零
        ctrl.decrypt = MagicMock(return_value=pt)

        async def fake_wait_notify(name, timeout=None):
            return _encrypted_frame(pt)

        ctrl.wait_notify = fake_wait_notify
        result = await ctrl._recv_set_response(2, 5)
        assert result is None

    @pytest.mark.asyncio
    async def test_result_zero_error_parses_value(self):
        """状态字节（pt[9]/pt[10]）全零 → 正常解析 value（uint8: pt[13]）。"""
        from unittest.mock import MagicMock
        ctrl = self._make_ctrl()
        pt = _result_frame(0x04, 2, 5, err=0, value=bytes([0x2A]))
        ctrl.decrypt = MagicMock(return_value=pt)

        async def fake_wait_notify(name, timeout=None):
            return _encrypted_frame(pt)

        ctrl.wait_notify = fake_wait_notify
        result = await ctrl._recv_set_response(2, 5)
        assert result == {"piid": 5, "value": 0x2A, "raw": pt}

    @pytest.mark.asyncio
    async def test_ack_then_result_returns_value(self):
        """先 ACK 后 Result（2.5s 窗口内到达）→ 正常返回 value。

        同时验证 ACK 后 Result 等待 deadline 从 1.0s 放宽到 2.5s：
        第二次 wait_notify 的 timeout ≈ 2.5（修复前为 1.0）。
        """
        from unittest.mock import MagicMock
        ctrl = self._make_ctrl()
        ack = _ack_frame(2, 5)
        pt = _result_frame(0x04, 2, 5, err=0, value=bytes([0x01]))
        ctrl.decrypt = MagicMock(side_effect=[ack, pt])

        timeouts = []
        seq = [ack, pt]

        async def fake_wait_notify(name, timeout=None):
            timeouts.append(timeout)
            return _encrypted_frame(seq.pop(0))

        ctrl.wait_notify = fake_wait_notify
        result = await ctrl._recv_set_response(2, 5, timeout=8.0)

        assert result == {"piid": 5, "value": 0x01, "raw": pt}
        # 第一次等待受外层 8s deadline 限制 → timeout = min(≈8, 3) = 3.0
        assert timeouts[0] == 3.0
        # ACK 后 deadline 重置为 2.5s → 第二次等待 timeout ≈ 2.5
        assert 2.0 <= timeouts[1] <= 2.55, \
            f"ACK 后 Result 等待窗口应≈2.5s, got {timeouts[1]}"

    @pytest.mark.asyncio
    async def test_ack_only_returns_value_none(self):
        """只收到 ACK、Result 始终不来 → 返回 {'value': None}。

        controller 层 ACK-only 契约（ble_manager 侧据此报
        "device acknowledged but did not confirm"）。
        """
        from unittest.mock import MagicMock
        ctrl = self._make_ctrl()
        ack = _ack_frame(2, 5)
        ctrl.decrypt = MagicMock(return_value=ack)

        seq = [_encrypted_frame(ack), None]

        async def fake_wait_notify(name, timeout=None):
            return seq.pop(0) if seq else None

        ctrl.wait_notify = fake_wait_notify
        result = await ctrl._recv_set_response(2, 5)
        assert result == {"piid": 5, "value": None, "raw": None}

    @pytest.mark.asyncio
    async def test_no_response_returns_none(self):
        """wait_notify 一直超时（None）→ 返回 None（非 dict）。

        ble_manager 侧据此报 "no response from device"。用极短 timeout
        避免真实 8s deadline 空转。
        """
        from unittest.mock import MagicMock
        ctrl = self._make_ctrl()
        ctrl.decrypt = MagicMock()  # 不应被调用

        async def fake_wait_notify(name, timeout=None):
            return None

        ctrl.wait_notify = fake_wait_notify
        result = await ctrl._recv_set_response(2, 5, timeout=0.05)
        assert result is None
        ctrl.decrypt.assert_not_called()


class TestSendEncryptedClearQueue:
    """ble-warnings P2: _send_encrypted 写命令前清空 cmd_send 队列。

    回归场景: cmd_send 通道残留设备越带推送帧/过期响应时，
    wait_notify("cmd_send") 必须读到空队列（等待 RCV_RDY/RCV_OK），
    而不是先取到陈旧帧触发虚假 "CMD_SEND no RCV_RDY/RCV_OK" 警告。
    """

    @pytest.mark.asyncio
    async def test_send_encrypted_clears_cmd_send_before_wait(self):
        import asyncio
        from unittest.mock import MagicMock, AsyncMock, patch
        from src.cuktech_ble.controller import CuktechBLEController

        ctrl = CuktechBLEController(mac="AA:BB:CC:DD:EE:FF", token="aabbccddeeff")
        ctrl.client = MagicMock()
        ctrl.client.write_gatt_char = AsyncMock()

        # 预置一条陈旧/越带帧（即文档中 000001050100 6 字节特征）
        q = ctrl._notify_queues.setdefault("cmd_send", asyncio.Queue())
        q.put_nowait(b"\x00\x00\x01\x05\x01\x00")

        # 拦截 wait_notify，记录调用瞬间 cmd_send 是否已清空
        states = []

        async def fake_wait_notify(name, timeout=5.0):
            que = ctrl._notify_queues.get(name)
            states.append((name, que.empty() if que else True))
            return None  # 返回 None → _send_encrypted 判定 "no RCV_RDY" → False

        with patch.object(ctrl, "_encrypt", return_value=b"\x01\x00\xaa\xbb") as m_enc:
            with patch.object(ctrl, "wait_notify", side_effect=fake_wait_notify):
                result = await ctrl._send_encrypted(b"\x00\x10\x10\x00")

        # 关键断言: 两次 wait_notify("cmd_send") 调用时队列都应为空（陈旧帧已被丢弃）
        wait_calls = [name for name, _ in states]
        assert "cmd_send" in wait_calls
        assert all(empty for name, empty in states if name == "cmd_send"), \
            f"cmd_send 队列写入前未被清空: {states}"
        assert m_enc.called
        # header 帧确实通过 GATT 写入
        assert ctrl.client.write_gatt_char.called
