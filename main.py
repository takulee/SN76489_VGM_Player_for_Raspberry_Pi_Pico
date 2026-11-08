import machine
machine.freq(250_000_000)     # 標準は125MHz。PWMの分周が細かくなり、クロックの誤差が小さくなる

from machine import Pin, PWM
import time
from time import ticks_us, ticks_diff, ticks_add, sleep_us, sleep_ms
import os

# ---- 再生設定 ----
VGM_DIR = "/vgm"          # VGMファイルを置いたフォルダ ("/" ならルート)
RECURSIVE = True          # True にするとサブフォルダ内のVGMも再生する
MAX_DEPTH = 4             # 何階層下まで探すか (0 = 指定フォルダのみ)
REPEAT_PLAYLIST = False   # True にすると全曲を再生し終えたあと最初から繰り返す
LOOP_COUNT = 1            # 1曲ごとのループ回数 (0=ループしない, -1=無限)
BUF_SIZE = 2048           # ファイル読み込みバッファ(バイト)
STARTUP_DELAY_MS = 1500   # 電源ON後、最初の曲を再生し始めるまでの無音時間(ms)

# ---- クロック ----
# "vgm"      : VGMファイルのヘッダのクロックに合わせて、曲ごとに出力周波数を変える
# "fixed"    : CLOCK_OUT_HZ で固定して出力する
# "external" : 外部水晶を使う(Picoからは出力しない)
CLOCK_MODE = "vgm"
PIN_CLOCK = 16            # クロックを出力するGPIO (物理21番)
CLOCK_OUT_HZ = 2000000    # "fixed" のときの周波数。"vgm" でも起動時と、曲のクロックが不明なときに使う
CLOCK_MIN_HZ = 1000000    # "vgm" のとき、この範囲に収めて出力する (データシートで動作範囲を確認して調整)
CLOCK_MAX_HZ = 4000000
CLOCK_EXT_HZ = 3579545    # "external" のときの、外部水晶の周波数
CLOCK_TOLERANCE = 0.003   # クロック比がこの範囲内なら、音程補正を省略する (0.003 = 0.3%)

# ---- 音程の調整 ----
PITCH_SHIFT = 0.0         # 音程を半音単位で上下 (+2.5 なら半音2.5個ぶん高く、負の値で低く。小数も可)
PITCH_BY_FILE = {}        # 曲ごとに変える場合: {"01_xxx.vgm": 2.5, "02_yyy": -1}

# ---- 音量の調整 (下げる方向のみ。上げることはできません) ----
MASTER_ATTEN = 0          # 全体の音量を下げる段数 (1段 = 約2dB、0=そのまま、15=無音)
CHANNEL_ATTEN = [         # チャンネルごとに追加で下げる段数 [トーン1, トーン2, トーン3, ノイズ]
    [0, 0, 0, 0],         # VGMの1個目(コマンド0x50)
    [0, 0, 0, 0],         # VGMの2個目(コマンド0x30)
]

# ---- チャンネルのミュート ----
# VGMの1個目 / 2個目それぞれについて、消音するチャンネルを指定
# (1,2,3 = トーン1〜3, 4 = ノイズ)  例: [1, 4] でトーン1とノイズを消音
MUTE_CH = [
    [],      # VGMの1個目(コマンド0x50)
    [],      # VGMの2個目(コマンド0x30)
]

# ---- テスト用の出力設定 ----
# 使う物理IC: "both"=2個とも / "first"=1個目だけ / "second"=2個目だけ
PLAY_MODE = "both"
# "first" / "second" のとき、VGMの何個目のチップのデータを鳴らすか
# (0=VGMの1個目(コマンド0x50), 1=VGMの2個目(コマンド0x30))
SINGLE_SOURCE = 0
# "both" のとき、VGMの1個目と2個目を物理IC側で入れ替える
SWAP_CHIPS = False

# ---- ピン設定 ----
DATA_PINS = [0, 1, 2, 3, 4, 5, 6, 7]   # データバスに使うGPIO (GP0..GP7)。2個で共通

# 各物理ICの制御ピン (WE, READY, CE)
CHIP_PINS = [
    (10, 11, 12),   # 1個目
    (13, 14, 15),   # 2個目
]

data = [Pin(p, Pin.OUT, value=0) for p in DATA_PINS]
we = [Pin(p[0], Pin.OUT, value=1) for p in CHIP_PINS]      # アクティブLow
ready = [Pin(p[1], Pin.IN) for p in CHIP_PINS]
ce = [Pin(p[2], Pin.OUT, value=1) for p in CHIP_PINS]      # アクティブLow (GND直結なら未接続でOK)


# ---- クロック出力 (PWMで50%デューティの矩形波) ----
CLOCK_HZ = CLOCK_EXT_HZ     # 実機のSN76489に実際に入っているクロック (出力する場合は読み戻した値に更新される)
clk_pwm = None


def set_clock(hz):
    """PicoのPWMでクロックを出力し、実際に出ている周波数を CLOCK_HZ に記録する"""
    global CLOCK_HZ
    clk_pwm.freq(hz)
    clk_pwm.duty_u16(32768)                 # デューティ50%
    CLOCK_HZ = clk_pwm.freq()               # 実際の周波数を読み戻す


if CLOCK_MODE == "external":
    pass
elif CLOCK_MODE in ("fixed", "vgm"):
    clk_pwm = PWM(Pin(PIN_CLOCK))
    set_clock(CLOCK_OUT_HZ)
    print("クロック出力: GP%d, 設定 %d Hz -> 実際 %d Hz" % (PIN_CLOCK, CLOCK_OUT_HZ, CLOCK_HZ))
else:
    raise ValueError('CLOCK_MODE は "vgm" / "fixed" / "external" のいずれかにしてください')


# ---- 出力設定の解決 ----
def make_route():
    """ROUTE[v] = VGMのv個目のデータを送る物理IC番号 (送らないなら None)"""
    if PLAY_MODE == "both":
        return [1, 0] if SWAP_CHIPS else [0, 1]
    if PLAY_MODE == "first":
        phys = 0
    elif PLAY_MODE == "second":
        phys = 1
    else:
        raise ValueError('PLAY_MODE は "both" / "first" / "second" のいずれかにしてください')
    route = [None, None]
    route[SINGLE_SOURCE] = phys
    return route


ROUTE = make_route()
ACTIVE = [0, 1] if PLAY_MODE == "both" else [0 if PLAY_MODE == "first" else 1]   # 使う物理IC


# ---- SN76489 低レベル制御 ----
def _wait_ready(pin, level, timeout_us=1000):
    t0 = ticks_us()
    while pin.value() != level:
        if ticks_diff(ticks_us(), t0) > timeout_us:
            return False
    return True


def sn_write(n, byte):
    """n番目(0または1)の物理ICへ1バイト書き込む(WE/READYハンドシェイク付き)"""
    for i, p in enumerate(data):
        p.value((byte >> (7 - i)) & 1)   # D0がMSB
    c = ce[n]
    w = we[n]
    rd = ready[n]
    c.value(0)
    w.value(0)                           # 書き込み開始
    _wait_ready(rd, 0)                   # READYがLow(受信中)
    _wait_ready(rd, 1)                   # READYがHigh(受付完了)
    w.value(1)                           # 書き込み終了
    c.value(1)


def mute_all():
    for n in ACTIVE:                     # 使う物理ICだけ消音
        for ch in range(4):
            sn_write(n, 0x90 | (ch << 5) | 0x0F)   # 減衰量15 = 無音


def startup_mute():
    """電源ON直後の不定状態を消音する。クロックが安定するまで待ち、念のため複数回書き込む"""
    time.sleep_ms(100)
    for _ in range(3):
        mute_all()
        time.sleep_ms(20)


# ---- 音程の補正(クロック差 + 音程シフト) ----
def make_writer(sn_clock, n, shift=0.0):
    """n番目の物理ICへ書き込む関数を返す。
    クロックの違いと音程シフトを、音程レジスタの値の補正でまとめて行う"""
    # N' = N * (実機クロック / ファイルクロック) / 2^(シフト/12)
    ratio = (CLOCK_HZ / sn_clock) * 2 ** (-shift / 12)
    if abs(ratio - 1) <= CLOCK_TOLERANCE:    # 補正が不要なら、そのまま書き込む
        def plain(b):
            sn_write(n, b)
        return plain

    R = int(ratio * 65536 + 0.5)            # 16bit固定小数
    tone = [0, 0, 0]                        # 各チャンネルの音程レジスタ(10bit)の記憶
    sent = [-1, -1, -1]                     # 実際に送った値(同じ値の再送を避ける)
    latched = [-1]                          # 最後にラッチされた音程チャンネル(-1=音程以外)

    def write(b):
        if b & 0x80:                        # ラッチバイト
            ch = (b >> 5) & 3
            if (b & 0x10) or ch == 3:       # 音量・ノイズはそのまま通す
                latched[0] = -1
                sn_write(n, b)
                return
            latched[0] = ch
            tone[ch] = (tone[ch] & 0x3F0) | (b & 0x0F)
        else:                               # データバイト
            ch = latched[0]
            if ch < 0:                      # 音量・ノイズの続きはそのまま通す
                sn_write(n, b)
                return
            tone[ch] = ((b & 0x3F) << 4) | (tone[ch] & 0x0F)

        v = tone[ch] or 1024                # レジスタ値0は1024扱い
        v = (v * R + 32768) >> 16           # 四捨五入しながらスケーリング
        if v < 1:
            v = 1
        elif v > 1024:
            v = 1024
        reg = v & 0x3FF                     # 1024は0で表す
        if reg != sent[ch]:
            sent[ch] = reg
            sn_write(n, 0x80 | (ch << 5) | (reg & 0x0F))
            sn_write(n, (reg >> 4) & 0x3F)

    return write


# ---- 音量の調整・ミュート ----
def make_volume_filter(sink, muted, ch_atten):
    """音量の書き込みに減衰量を足して音量を下げるフィルタ(ミュートは減衰量15)。
    muted は 1〜4 の番号のリスト (1,2,3=トーン, 4=ノイズ)。ch_atten は [トーン1,トーン2,トーン3,ノイズ]"""
    add = [0, 0, 0, 0]
    for ch in range(4):
        a = MASTER_ATTEN + ch_atten[ch]
        add[ch] = 0 if a < 0 else (15 if a > 15 else a)
    for ch in muted:
        if 1 <= ch <= 4:
            add[ch - 1] = 15
    if add == [0, 0, 0, 0]:
        return sink                          # 調整なしなら何もしない

    latched = [-1]      # 最後にラッチされた「減衰を足すチャンネルの音量レジスタ」(-1=それ以外)

    def write(b):
        if b & 0x80:                         # ラッチバイト
            ch = (b >> 5) & 3
            if (b & 0x10) and add[ch]:       # 減衰を足すチャンネルの音量
                att = (b & 0x0F) + add[ch]
                if att > 15:
                    att = 15
                latched[0] = ch
                sink((b & 0xF0) | att)
                return
            latched[0] = -1
            sink(b)
        else:                                # データバイト
            ch = latched[0]
            if ch >= 0:                      # 音量の続き
                att = (b & 0x0F) + add[ch]
                if att > 15:
                    att = 15
                sink((b & 0x70) | att)
            else:
                sink(b)

    return write


# ---- ファイル読み込み(バッファ付き・バッファ使い回し) ----
class Reader:
    def __init__(self, f):
        self.f = f
        self.buf = bytearray(BUF_SIZE)
        self.n = 0
        self.i = 0

    def seek(self, pos):
        self.f.seek(pos)
        self.n = 0
        self.i = 0

    def byte(self):
        """1バイト読む。ファイル終端なら -1"""
        if self.i >= self.n:
            self.n = self.f.readinto(self.buf) or 0
            self.i = 0
            if not self.n:
                return -1
        b = self.buf[self.i]
        self.i += 1
        return b

    def skip(self, n):
        avail = self.n - self.i
        if n <= avail:
            self.i += n
        else:
            self.f.seek(n - avail, 1)
            self.n = 0
            self.i = 0

    def u16(self):
        lo = self.byte()
        hi = self.byte()
        return lo | (hi << 8)

    def u32(self):
        v = 0
        for s in (0, 8, 16, 24):
            v |= self.byte() << s
        return v


def u32(b, o):
    return int.from_bytes(b[o:o + 4], "little")


def operand_len(c):
    """SN76489以外のコマンドを読み飛ばすためのオペランド長"""
    if 0x30 <= c <= 0x3F: return 1
    if 0x40 <= c <= 0x4E: return 2
    if c == 0x4F or c == 0x50: return 1   # Game Gear PSGステレオ / 1個目のSN76489
    if 0x51 <= c <= 0x5F: return 2
    if c == 0x68: return 11
    if c == 0x90 or c == 0x91 or c == 0x95: return 4
    if c == 0x92: return 5
    if c == 0x93: return 10
    if c == 0x94: return 1
    if 0xA0 <= c <= 0xBF: return 2
    if 0xC0 <= c <= 0xDF: return 3
    if 0xE0 <= c <= 0xFF: return 4
    return 0


# ---- VGM再生 ----
def play_vgm(path):
    with open(path, "rb") as f:
        hdr = f.read(0x40)
        if hdr[:2] == b"\x1f\x8b":
            print("VGZ(gzip圧縮)です。解凍して .vgm にしてください")
            return
        if len(hdr) < 0x40 or hdr[:4] != b"Vgm ":
            print("VGMファイルではありません")
            return

        ver = u32(hdr, 0x08)
        clk_field = u32(hdr, 0x0C)
        sn_clock = clk_field & 0x3FFFFFFF
        dual = bool(clk_field & 0x40000000)      # bit30: SN76489が2個
        if sn_clock == 0 and ver < 0x101:
            sn_clock = 3579545
        if sn_clock == 0:
            print("このファイルにSN76489のデータはありません")
            return

        # クロックをVGMに合わせる (直前に消音済みの状態で切り替える)
        if CLOCK_MODE == "vgm":
            target = min(max(sn_clock, CLOCK_MIN_HZ), CLOCK_MAX_HZ)
            set_clock(target)
            sleep_ms(10)                         # クロックが安定するまで待つ
            print("クロック出力をVGMに合わせました: %d Hz -> 実際 %d Hz" % (target, CLOCK_HZ))

        # この曲の音程シフト量(半音)
        shift = PITCH_BY_FILE.get(path, PITCH_BY_FILE.get(path.rsplit("/", 1)[-1], PITCH_SHIFT))
        
        # VGMのv個目のデータを送る書き込み関数 (送り先がない/ファイルにそのチップがなければ None)
        # 流れ: VGMのデータ -> 音量調整・ミュート -> 音程補正 -> 物理ICへ書き込み
        w = [None, None]
        for v in (0, 1):
            phys = ROUTE[v]
            if phys is not None and (v == 0 or dual):
                w[v] = make_volume_filter(make_writer(sn_clock, phys, shift),
                                          MUTE_CH[v], CHANNEL_ATTEN[v])
        w0, w1 = w

        print("VGM ver %x, SN76489 clock %d Hz, ファイルのチップ数 %d"
              % (ver, sn_clock, 2 if dual else 1))
        print("設定: PLAY_MODE=%s, SINGLE_SOURCE=%d, SWAP_CHIPS=%s -> VGM1個目: %s, VGM2個目: %s"
              % (PLAY_MODE, SINGLE_SOURCE, SWAP_CHIPS,
                 ("物理IC%d" % (ROUTE[0] + 1)) if w0 else "出力なし",
                 ("物理IC%d" % (ROUTE[1] + 1)) if w1 else "出力なし"))
        if w0 is None and w1 is None:
            print("再生できるデータがありません(このファイルにその番号のチップがありません)")
            return
        if abs(CLOCK_HZ / sn_clock - 1) > CLOCK_TOLERANCE:
            print("クロック補正あり: ファイル %d Hz -> 実機 %d Hz" % (sn_clock, CLOCK_HZ))
        if shift != 0:
            print("音程シフト:", shift, "半音 (ノイズの固定周波数は変わりません)")
        if MASTER_ATTEN != 0:
            print("マスター音量: -%d段 (約-%d dB)" % (MASTER_ATTEN, MASTER_ATTEN * 2))
        for v in (0, 1):
            if w[v]:
                if any(CHANNEL_ATTEN[v]):
                    print("チャンネル別の減衰: VGMの%d個目" % (v + 1), CHANNEL_ATTEN[v])
                if MUTE_CH[v]:
                    print("ミュート: VGMの%d個目 チャンネル" % (v + 1), MUTE_CH[v])

        # データ開始位置
        data_start = 0x40
        if ver >= 0x150:
            off = u32(hdr, 0x34)
            if off:
                data_start = 0x34 + off

        # ループ位置
        loop_off = u32(hdr, 0x1C)
        loop_pos = (0x1C + loop_off) if loop_off else None

        r = Reader(f)
        r.seek(data_start)

        # 時間管理: 44100Hz基準のサンプル数 -> マイクロ秒
        t_ref = ticks_us()
        acc = 0

        def wait(n):
            nonlocal t_ref, acc
            acc += n
            target = ticks_add(t_ref, acc * 10000 // 441)
            d = ticks_diff(target, ticks_us())
            if d > 1500:
                sleep_us(d - 1000)
            while ticks_diff(target, ticks_us()) > 0:
                pass
            if acc >= 441000:
                chunk = acc // 441 * 441
                t_ref = ticks_add(t_ref, chunk // 441 * 10000)
                acc -= chunk

        len62, len63 = 735, 882
        loops_left = LOOP_COUNT

        mute_all()
        while True:
            c = r.byte()
            if c < 0:
                break
            if c == 0x50:                       # VGMの1個目のSN76489
                b = r.byte()
                if w0:
                    w0(b)
            elif c == 0x30:                     # VGMの2個目のSN76489
                b = r.byte()
                if w1:
                    w1(b)
            elif c == 0x61:
                wait(r.u16())
            elif c == 0x62:
                wait(len62)
            elif c == 0x63:
                wait(len63)
            elif 0x70 <= c <= 0x7F:
                wait((c & 0x0F) + 1)
            elif 0x80 <= c <= 0x8F:             # YM2612 DAC書き込み+待ち(待ちだけ守る)
                wait(c & 0x0F)
            elif c == 0x64:                     # 0x62/0x63の待ち時間変更
                cc = r.byte()
                n = r.u16()
                if cc == 0x62:
                    len62 = n
                elif cc == 0x63:
                    len63 = n
            elif c == 0x66:                     # 終端 / ループ
                if loop_pos is not None and loops_left != 0:
                    if loops_left > 0:
                        loops_left -= 1
                    r.seek(loop_pos)
                else:
                    break
            elif c == 0x67:                     # データブロック(PCMなど)は読み飛ばす
                r.byte()
                r.byte()
                r.skip(r.u32())
            else:
                r.skip(operand_len(c))          # 他の音源ICのコマンドはここで読み飛ばす


# ---- プレイリスト (拡張子ではなく中身で判定) ----
def is_vgm(path):
    """ファイルの先頭4バイトで VGM かどうかを判定する"""
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"Vgm "
    except OSError:
        return False


def list_vgm(folder, depth=0):
    """フォルダ内(RECURSIVE=Trueならサブフォルダも)の VGM ファイルを、名前順に並べて返す。
    サブフォルダは、名前順の位置でその中身を展開する"""
    try:
        names = os.listdir(folder)
    except OSError:
        print("フォルダが見つかりません:", folder)
        return []
    files = []
    for nm in sorted(names):
        if nm.startswith("."):                  # 隠しファイル・フォルダは除外
            continue
        path = folder.rstrip("/") + "/" + nm
        try:
            is_dir = os.stat(path)[0] & 0x4000
        except OSError:
            continue
        if is_dir:
            if RECURSIVE and depth < MAX_DEPTH:
                files.extend(list_vgm(path, depth + 1))
        elif is_vgm(path):
            files.append(path)
    return files


def play_all(folder=VGM_DIR):
    while True:
        files = list_vgm(folder)
        if not files:
            print("VGMファイルが見つかりません:", folder)
            return
        for i, path in enumerate(files):
            print("[%d/%d] %s" % (i + 1, len(files), path))
            try:
                play_vgm(path)
            except OSError as e:
                print("読み込みエラー:", path, e)
            mute_all()                    # 曲の切れ目で音が残らないように
            time.sleep_ms(500)
        if not REPEAT_PLAYLIST:
            break


try:
    startup_mute()                    # まず消音
    time.sleep_ms(STARTUP_DELAY_MS)   # 無音のまま少し待ってから再生開始
    play_all()
finally:
    mute_all()
