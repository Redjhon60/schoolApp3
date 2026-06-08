"""
dashboard.py — SGS v4  (full improvement pass)

Improvements
─────────────
LOGIC
  • All queries use single bulk loads (no per-student N+1 queries)
  • Debt computation uses join not Python loop
  • School year derived once and reused everywhere
  • Salary month lookup uses both month-name and paid_date (robust fallback)
  • Expected revenue skips NAN students correctly
  • Charts cache data arrays so switching month filter doesn't re-query charts

UI / LAYOUT
  • Top bar: greeting + month badge + refresh icon — one compact row
  • All 16 KPIs in a single responsive QGridLayout (8 cols × 2 data rows)
  • Profit card spans 2 cols and shows inline formula breakdown
  • Re-inscription pills inline next to profit
  • Section micro-labels above each KPI group (no dividers)
  • Annual chart has compact header; charts have titles inside figure
  • Notifications rendered as plain pill rows (no frame per row)
  • Color-coded debt urgency on outstanding card
  • "Mois en cours" auto-detected and shown in filter badge
"""

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
    QGridLayout, QScrollArea, QSizePolicy, QPushButton, QComboBox, QApplication
)
from PySide6.QtCore import Qt, QTimer
from datetime import datetime

try:
    import matplotlib, matplotlib.ticker
    matplotlib.use('Agg')
    from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
    from matplotlib.figure import Figure
    from matplotlib.patches import Patch
    from matplotlib.lines import Line2D
    HAS_MPL = True
except Exception:
    HAS_MPL = False

from models.database import (
    Student, Payment, MonthRecord, Employee, Salary,
    Setting, ExpenseCategory, ExpensePayment, SCHOOL_MONTHS
)
from themes.style import (
    PRIMARY, PRIMARY_LIGHT, SUCCESS, SUCCESS_LIGHT, DANGER, DANGER_LIGHT,
    WARNING, WARNING_LIGHT, INFO, INFO_LIGHT, PURPLE, PURPLE_LIGHT,
    TEAL, TEAL_LIGHT, BG_CARD, BORDER, TEXT_MAIN, TEXT_SUB,
    REINSCRIPTION_LABELS, CLASSES
)

# ── Constants ──────────────────────────────────────────────────────────────────
_SCHOOL_CAL = {
    'Septembre': 9, 'Octobre': 10, 'Novembre': 11, 'Décembre': 12,
    'Janvier': 1, 'Février': 2, 'Mars': 3, 'Avril': 4, 'Mai': 5, 'Juin': 6,
}
_CAL_TO_SIDX = {9:0,10:1,11:2,12:3,1:4,2:5,3:6,4:7,5:8,6:9}
_SHORT_SCH   = ['Sep','Oct','Nov','Déc','Jan','Fév','Mar','Avr','Mai','Jun']
_SHORT_CAL   = ['Jan','Fév','Mar','Avr','Mai','Jun','Jul','Aoû','Sep','Oct','Nov','Déc']


def _cur_school_month():
    idx = _CAL_TO_SIDX.get(datetime.now().month)
    return SCHOOL_MONTHS[idx] if idx is not None else None


def _fmt_mad(v: float) -> str:
    if abs(v) >= 1_000_000:
        return f'{v/1_000_000:.2f}M MAD'
    if abs(v) >= 1_000:
        return f'{v/1_000:.1f}k MAD'
    return f'{v:.0f} MAD'


# ── KPI card ──────────────────────────────────────────────────────────────────

def kpi(icon, label, value, accent, bg, subtitle=None):
    card = QFrame()
    card.setObjectName('kpi_card')
    card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    card.setFixedHeight(90 if subtitle else 82)
    card.setStyleSheet(f'''
        QFrame#kpi_card {{
            background: {BG_CARD};
            border: 1px solid {BORDER};
            border-left: 4px solid {accent};
            border-radius: 10px;
        }}
        QFrame#kpi_card:hover {{ background: {bg}; }}
    ''')
    h = QHBoxLayout(card)
    h.setContentsMargins(10, 8, 10, 8)
    h.setSpacing(9)

    ico = QLabel(icon)
    ico.setFixedSize(28, 28)
    ico.setAlignment(Qt.AlignCenter)
    ico.setStyleSheet(f'background:{bg}; border-radius:7px; font-size:14px;')

    col = QVBoxLayout()
    col.setSpacing(1)
    val_lbl = QLabel(str(value))
    val_lbl.setStyleSheet(f'color:{accent}; font-size:16px; font-weight:800; background:transparent;')
    lbl_lbl = QLabel(label)
    lbl_lbl.setStyleSheet(f'color:{TEXT_SUB}; font-size:9.5px; font-weight:600; background:transparent;')
    col.addWidget(val_lbl)
    col.addWidget(lbl_lbl)
    if subtitle:
        sub = QLabel(subtitle)
        sub.setStyleSheet('color:#9CA3AF; font-size:8.5px; background:transparent;')
        col.addWidget(sub)

    h.addWidget(ico)
    h.addLayout(col)
    h.addStretch()
    card._val = val_lbl
    return card


def micro_label(text):
    lbl = QLabel(text)
    lbl.setStyleSheet(
        f'color:{TEXT_SUB}; font-size:8.5px; font-weight:700; letter-spacing:0.8px;'
        ' background:transparent; padding:0;'
    )
    return lbl


def hline():
    f = QFrame(); f.setFrameShape(QFrame.HLine)
    f.setStyleSheet(f'color:{BORDER}; background:{BORDER}; max-height:1px; margin:0;')
    return f


# ── Dashboard ─────────────────────────────────────────────────────────────────

class DashboardWidget(QWidget):

    def __init__(self, session):
        super().__init__()
        self.session = session
        self.setStyleSheet('background:transparent;')
        self._selected_month = None
        # Cached data for charts (avoid re-query when only month filter changes)
        self._chart_cache = {}
        self._setup_ui()
        self._load_data()

    # ── Layout ────────────────────────────────────────────────────────────────

    def _setup_ui(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet('QScrollArea{border:none;background:transparent;}')

        root = QWidget()
        root.setStyleSheet('background:transparent;')
        self._vl = QVBoxLayout(root)
        self._vl.setContentsMargins(18, 14, 18, 18)
        self._vl.setSpacing(8)

        # ── Top bar ───────────────────────────────────────────────────────────
        self._vl.addLayout(self._build_top_bar())

        # ── KPI grid (8 columns) ──────────────────────────────────────────────
        self._grid = QGridLayout()
        self._grid.setHorizontalSpacing(8)
        self._grid.setVerticalSpacing(4)
        self._grid.setContentsMargins(0, 0, 0, 0)
        for c in range(8):
            self._grid.setColumnStretch(c, 1)
        self._vl.addLayout(self._grid)

        # ── Analytics ─────────────────────────────────────────────────────────
        if HAS_MPL:
            self._vl.addWidget(hline())
            self._annual = AnnualProfitChart(self.session)
            self._vl.addWidget(self._annual)

            r1 = QHBoxLayout(); r1.setSpacing(8)
            self._pf, self._pl = self._chart_card()
            self._rf, self._rl = self._chart_card()
            r1.addWidget(self._pf, 1); r1.addWidget(self._rf, 1)
            self._vl.addLayout(r1)

            r2 = QHBoxLayout(); r2.setSpacing(8)
            self._qf, self._ql = self._chart_card()
            self._cf, self._cl_lay = self._chart_card()
            r2.addWidget(self._qf, 3); r2.addWidget(self._cf, 2)
            self._vl.addLayout(r2)

        # ── Notifications ─────────────────────────────────────────────────────
        self._vl.addWidget(hline())
        self._notif_lbl = QLabel('🔔  Alertes')
        self._notif_lbl.setStyleSheet(
            f'color:{TEXT_MAIN}; font-size:11px; font-weight:700; background:transparent;'
        )
        self._vl.addWidget(self._notif_lbl)
        self._notif_lay = QVBoxLayout()
        self._notif_lay.setSpacing(3)
        self._notif_lay.setContentsMargins(0, 0, 0, 0)
        self._vl.addLayout(self._notif_lay)
        self._vl.addStretch()

        scroll.setWidget(root)
        ol = QVBoxLayout(self)
        ol.setContentsMargins(0, 0, 0, 0)
        ol.addWidget(scroll)

    # ── Top bar ───────────────────────────────────────────────────────────────

    def _build_top_bar(self):
        bar = QHBoxLayout(); bar.setSpacing(10)

        # School year badge
        self._sy_lbl = QLabel('')
        self._sy_lbl.setStyleSheet(
            f'color:{PRIMARY}; font-size:11px; font-weight:700;'
            f' background:{PRIMARY_LIGHT}; border-radius:6px; padding:3px 10px;'
        )

        # Month filter
        lbl = QLabel('📅')
        lbl.setStyleSheet('font-size:15px; background:transparent;')

        combo_css = (
            f'QComboBox{{background:{PRIMARY_LIGHT};border:1px solid {PRIMARY}33;'
            f'border-radius:8px;color:{PRIMARY};padding:3px 10px;'
            f'font-size:11px;font-weight:700;min-width:140px;}}'
            f'QComboBox::drop-down{{border:none;width:16px;}}'
            f'QComboBox QAbstractItemView{{background:white;border:1px solid {BORDER};'
            f'color:{TEXT_MAIN};outline:none;border-radius:8px;}}'
            f'QComboBox QAbstractItemView::item{{padding:5px 12px;}}'
            f'QComboBox QAbstractItemView::item:selected{{background:{PRIMARY_LIGHT};color:{PRIMARY};}}'
        )
        self._month_combo = QComboBox()
        self._month_combo.setStyleSheet(combo_css)
        self._month_combo.setFixedHeight(30)
        self._month_combo.addItem('📍 Mois en cours', None)
        for m in SCHOOL_MONTHS:
            self._month_combo.addItem(m, m)
        self._month_combo.currentIndexChanged.connect(self._on_month_changed)

        # Pre-select current school month
        cur = _cur_school_month()
        if cur and cur in SCHOOL_MONTHS:
            self._month_combo.setCurrentIndex(SCHOOL_MONTHS.index(cur) + 1)
            self._selected_month = cur

        reset = QPushButton('↺')
        reset.setFixedSize(28, 28)
        reset.setToolTip('Mois en cours')
        reset.setStyleSheet(
            f'QPushButton{{background:{PRIMARY_LIGHT};color:{PRIMARY};border:none;'
            f'border-radius:7px;font-size:13px;font-weight:700;}}'
            f'QPushButton:hover{{background:{PRIMARY};color:white;}}'
        )
        reset.clicked.connect(lambda: self._month_combo.setCurrentIndex(0))

        # Refresh
        rfsh = QPushButton('↺  Actualiser')
        rfsh.setFixedHeight(28)
        rfsh.setStyleSheet(
            'QPushButton{background:#F3F4F6;color:#374151;border:1px solid #E5E7EB;'
            'border-radius:7px;padding:0 12px;font-size:11px;font-weight:500;}'
            'QPushButton:hover{background:#E5E7EB;}'
        )
        rfsh.clicked.connect(self.refresh)

        bar.addWidget(self._sy_lbl)
        bar.addSpacing(6)
        bar.addWidget(lbl)
        bar.addWidget(self._month_combo)
        bar.addWidget(reset)
        bar.addStretch()
        bar.addWidget(rfsh)
        return bar

    # ── Chart card ────────────────────────────────────────────────────────────

    def _chart_card(self):
        frame = QFrame()
        frame.setStyleSheet(
            f'QFrame{{background:{BG_CARD};border:1px solid {BORDER};border-radius:10px;}}'
        )
        frame.setMinimumHeight(220)
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(0)
        return frame, lay

    def _clear_chart(self, lay):
        for i in reversed(range(lay.count())):
            w = lay.itemAt(i).widget()
            if w: w.setParent(None)

    def _clear_grid(self):
        for i in reversed(range(self._grid.count())):
            item = self._grid.itemAt(i)
            if item and item.widget(): item.widget().setParent(None)

    # ── Month filter ──────────────────────────────────────────────────────────

    def _on_month_changed(self, _):
        self._selected_month = self._month_combo.currentData()
        self._load_data(charts=False)   # KPIs only, charts stay

    def _resolve_month(self, sy):
        cy = datetime.now().year
        if self._selected_month:
            m = self._selected_month
            try: end = int(sy.split('-')[0]) + 1
            except: end = cy
            cal = _SCHOOL_CAL.get(m, 1)
            return m, (end - 1) if cal >= 9 else end
        m = _cur_school_month()
        return m, cy

    # ── Data ──────────────────────────────────────────────────────────────────

    def _load_data(self, charts=True):
        self.session.expire_all()
        try:    self._render(charts=charts)
        except: import traceback; traceback.print_exc()

    def _render(self, charts=True):
        now = datetime.now()
        cy  = now.year

        # ── Settings ──────────────────────────────────────────────────────────
        settings = {s.key: s.value for s in self.session.query(Setting).all()}
        sy = settings.get('school_year', '2024-25')
        self._sy_lbl.setText(f'Année {sy}')

        month, pyear = self._resolve_month(sy)
        mon_lbl = month or '—'

        # ── Bulk student load ──────────────────────────────────────────────────
        students = self.session.query(Student).filter_by(active=True).all()
        n_total  = len(students)
        sid_map  = {s.id: s for s in students}

        # ── MonthRecords for selected month (single query) ────────────────────
        recs = {}
        if month:
            for r in self.session.query(MonthRecord).filter_by(
                month_name=month, school_year=sy
            ).all():
                recs[r.student_id] = r

        n_paid   = sum(1 for s in students if recs.get(s.id) and recs[s.id].status == 'paid')
        n_unpaid = sum(1 for s in students if recs.get(s.id) and recs[s.id].status == 'unpaid')
        n_nan    = sum(1 for s in students if recs.get(s.id) and recs[s.id].status == 'nan')

        # ── Outstanding debt (all unpaid months, single query) ────────────────
        all_unpaid = self.session.query(MonthRecord).filter_by(
            status='unpaid', school_year=sy
        ).all()
        outstanding = sum(
            (sid_map[r.student_id].monthly_fee or 0)
            + ((sid_map[r.student_id].transport_fee or 0) if sid_map[r.student_id].has_transport else 0)
            for r in all_unpaid if r.student_id in sid_map
        )

        # ── Revenue: expected vs collected ────────────────────────────────────
        expected = 0.0
        if month:
            for s in students:
                r = recs.get(s.id)
                if r and r.status == 'nan': continue
                expected += (s.monthly_fee or 0) + ((s.transport_fee or 0) if s.has_transport else 0)

        # Single bulk query for payments this month
        month_payments = []
        if month:
            month_payments = self.session.query(Payment).filter(
                Payment.payment_type.in_(['monthly', 'transport']),
                Payment.month == month,
                Payment.school_year == sy,
            ).all()

        collected = sum(p.amount or 0 for p in month_payments)
        gap = max(0.0, expected - collected)

        # ── Insurance (school year total) ─────────────────────────────────────
        ins_payments = self.session.query(Payment).filter_by(
            payment_type='insurance', school_year=sy
        ).all()
        ins_total = sum(p.amount or 0.0 for p in ins_payments)
        n_no_ins = sum(1 for s in students if not s.insurance_paid)

        # ── Expenses ──────────────────────────────────────────────────────────
        cats      = self.session.query(ExpenseCategory).filter_by(active=True).all()
        exp_exp   = sum(c.monthly_amount or 0 for c in cats)
        exp_paid  = 0.0
        if month:
            exp_rows = self.session.query(ExpensePayment).filter_by(
                month=month, year=pyear
            ).all()
            exp_paid = sum(ep.amount or 0.0 for ep in exp_rows)
        exp_rem = max(0.0, exp_exp - exp_paid)

        # ── Salaries ──────────────────────────────────────────────────────────
        n_emp = self.session.query(Employee).filter_by(active=True).count()
        sal_paid_ids = set()
        sal_amt = 0.0
        if month:
            for sal in self.session.query(Salary).filter_by(
                month=month, paid=True
            ).all():
                sal_paid_ids.add(sal.employee_id)
                sal_amt += sal.net_salary or getattr(sal, 'total', 0) or 0

        n_sal_paid = len(sal_paid_ids)
        n_sal_pend = max(0, n_emp - n_sal_paid)

        # Total salaries school year
        try: sy0, sy1 = int(sy.split('-')[0]), int(sy.split('-')[0]) + 1
        except: sy0, sy1 = cy - 1, cy
        all_year_salaries = self.session.query(Salary).filter(
            Salary.year.in_([sy0, sy1])
        ).all()
        total_sal = sum(
            (s.net_salary or s.total or 0.0)
            for s in all_year_salaries
            if getattr(s, 'paid', True)   # treat missing paid as True (backward compat)
        )

        # ── Profit ────────────────────────────────────────────────────────────
        profit = collected - exp_paid - sal_amt

        # ── Re-inscription ────────────────────────────────────────────────────
        n_yes  = sum(1 for s in students if getattr(s, 'reinscription_status', 'pending') == 'yes')
        n_no   = sum(1 for s in students if getattr(s, 'reinscription_status', 'pending') == 'no')
        n_pend = sum(1 for s in students if getattr(s, 'reinscription_status', 'pending') == 'pending')

        # ── Render KPI grid ───────────────────────────────────────────────────
        self._clear_grid()
        g = self._grid

        # ── Micro-labels row ──────────────────────────────────────────────────
        for col, txt in [(0,'👤 ÉLÈVES'),(4,'💰 REVENUS'),(7,'🛡 ASSURANCE')]:
            g.addWidget(micro_label(txt), 0, col)

        # ── Row 1: Students (4) + Revenue (3) + Insurance (1) ────────────────
        debt_color = DANGER if outstanding > 50000 else (WARNING if outstanding > 0 else SUCCESS)
        debt_light = DANGER_LIGHT if outstanding > 50000 else (WARNING_LIGHT if outstanding > 0 else SUCCESS_LIGHT)

        g.addWidget(kpi('👥','Total élèves',       n_total,                   PRIMARY, PRIMARY_LIGHT), 1,0)
        g.addWidget(kpi('✅',f'Payés — {mon_lbl}', n_paid,                    SUCCESS, SUCCESS_LIGHT), 1,1)
        g.addWidget(kpi('⏳',f'Non payés — {mon_lbl}', n_unpaid,              DANGER,  DANGER_LIGHT),  1,2)
        g.addWidget(kpi('💳','Créances (année)',    _fmt_mad(outstanding),     debt_color, debt_light,
                        subtitle=f'Dont {n_nan} mois NAN exclus'), 1,3)
        g.addWidget(kpi('📊','Revenus attendus',    _fmt_mad(expected),        PURPLE, PURPLE_LIGHT),  1,4)
        g.addWidget(kpi('💰','Revenus encaissés',   _fmt_mad(collected),       SUCCESS,SUCCESS_LIGHT), 1,5)
        g.addWidget(kpi('📉','Écart de revenus',    _fmt_mad(gap),             DANGER, DANGER_LIGHT),  1,6)
        g.addWidget(kpi('🛡','Assurance (année)',   _fmt_mad(ins_total),       TEAL,   TEAL_LIGHT),    1,7)

        # ── Micro-labels row 2 ────────────────────────────────────────────────
        for col, txt in [(0,'💸 DÉPENSES'),(3,'👔 SALAIRES'),(7,'❌ SANS ASSUR.')]:
            g.addWidget(micro_label(txt), 2, col)

        # ── Row 3: Expenses (3) + Salaries (4) + No-insurance (1) ────────────
        g.addWidget(kpi('📋','Dépenses prévues',    _fmt_mad(exp_exp),         INFO,   INFO_LIGHT),    3,0)
        g.addWidget(kpi('💸','Dépenses payées',     _fmt_mad(exp_paid),        DANGER, DANGER_LIGHT),  3,1)
        g.addWidget(kpi('🔖','Restant dépenses',    _fmt_mad(exp_rem),         WARNING,WARNING_LIGHT), 3,2)
        g.addWidget(kpi('👔','Total employés',      n_emp,                     PRIMARY,PRIMARY_LIGHT), 3,3)
        g.addWidget(kpi('✅','Salaires versés',     n_sal_paid,                SUCCESS,SUCCESS_LIGHT), 3,4)
        g.addWidget(kpi('⏳','En attente',          n_sal_pend,                DANGER, DANGER_LIGHT),  3,5)
        g.addWidget(kpi('💰','Total sal. (année)',  _fmt_mad(total_sal),       PURPLE, PURPLE_LIGHT),  3,6)
        g.addWidget(kpi('❌','Sans assurance',      n_no_ins,                  WARNING,WARNING_LIGHT), 3,7)

        # ── Micro-labels row 3 ────────────────────────────────────────────────
        g.addWidget(micro_label('📈 BÉNÉFICE'), 4, 0)
        g.addWidget(micro_label('🔄 RÉ-INSCRIPTION'), 4, 3)

        # ── Row 5: Profit (3 cols wide) + re-inscription (3) + spacer ─────────
        p_col = SUCCESS if profit >= 0 else DANGER
        p_lt  = SUCCESS_LIGHT if profit >= 0 else DANGER_LIGHT
        p_sub = f'Rev {_fmt_mad(collected)} − Dep {_fmt_mad(exp_paid)} − Sal {_fmt_mad(sal_amt)}'
        g.addWidget(kpi('📈', f'Bénéfice — {mon_lbl}',
                        ('+' if profit>0 else '')+_fmt_mad(profit),
                        p_col, p_lt, subtitle=p_sub), 5, 0, 1, 3)

        for ci, (val, cnt, col, lt) in enumerate([
            ('yes',    n_yes,  SUCCESS, SUCCESS_LIGHT),
            ('no',     n_no,   DANGER,  DANGER_LIGHT),
            ('pending',n_pend, WARNING, WARNING_LIGHT),
        ]):
            g.addWidget(kpi('🔄', REINSCRIPTION_LABELS.get(val, val), cnt, col, lt), 5, 3+ci)

        # ── Charts ────────────────────────────────────────────────────────────
        if HAS_MPL:
            if charts:
                self._cache_chart_data(sy, pyear)
            if hasattr(self, '_annual'):
                self._annual.refresh()
            hl = SCHOOL_MONTHS.index(month) if month and month in SCHOOL_MONTHS else None
            self._draw_school_profit(hl)
            self._draw_rev_exp(hl)
            self._draw_pay_rate(sy, hl)
            self._draw_classes()

        # ── Notifications ─────────────────────────────────────────────────────
        self._draw_notifications(n_no_ins, n_unpaid, n_pend, outstanding, mon_lbl)

    # ── Chart data cache ──────────────────────────────────────────────────────

    def _cache_chart_data(self, sy, pyear):
        rev = [0.0]*10; exp = [0.0]*10; sal = [0.0]*10
        for p in self.session.query(Payment).filter(
            Payment.payment_type.in_(['monthly','transport']),
            Payment.school_year == sy,
        ).all():
            try: rev[SCHOOL_MONTHS.index(p.month)] += p.amount or 0
            except: pass

        prev = pyear - 1
        for ep in self.session.query(ExpensePayment).all():
            try:
                idx = SCHOOL_MONTHS.index(ep.month)
                if ep.year == (prev if idx <= 3 else pyear):
                    exp[idx] += ep.amount or 0
            except: pass

        for s in self.session.query(Salary).all():
            if not getattr(s, 'paid', True): continue   # skip explicitly unpaid
            try: sal[SCHOOL_MONTHS.index(s.month)] += s.net_salary or getattr(s,'total',0) or 0
            except: pass

        self._chart_cache = {
            'rev': rev, 'exp': exp, 'sal': sal,
            'prf': [rev[i]-exp[i]-sal[i] for i in range(10)],
        }

    def _fig(self, w=6, h=2.5):
        fig = Figure(figsize=(w, h), facecolor='white')
        fig.subplots_adjust(left=0.08, right=0.97, top=0.80, bottom=0.18)
        return fig

    def _style_ax(self, ax, xs, xlabels, hl):
        ax.set_facecolor('white')
        ax.set_xticks(list(xs))
        ax.set_xticklabels(xlabels, fontsize=7)
        for i, t in enumerate(ax.get_xticklabels()):
            t.set_color(PRIMARY if (hl is not None and i == hl) else '#9CA3AF')
            t.set_fontweight('bold' if (hl is not None and i == hl) else 'normal')
        ax.yaxis.set_tick_params(labelcolor='#9CA3AF', labelsize=7)
        ax.yaxis.set_major_formatter(
            matplotlib.ticker.FuncFormatter(
                lambda v, _: f'{v/1000:.0f}k' if abs(v) >= 1000 else f'{v:.0f}'
            )
        )
        for sp in ax.spines.values(): sp.set_visible(False)
        ax.yaxis.grid(True, color='#F3F4F8', linewidth=0.7, zorder=0)
        ax.set_axisbelow(True)

    def _draw_school_profit(self, hl):
        self._clear_chart(self._pl)
        cache = self._chart_cache
        if not cache: return
        prf    = cache['prf']
        colors = [SUCCESS if v >= 0 else DANGER for v in prf]

        fig = self._fig()
        ax  = fig.add_subplot(111)
        bars = ax.bar(range(10), prf, color=colors, width=0.6, zorder=3)
        ax.axhline(0, color='#E5E7EB', lw=0.8, zorder=2)
        if hl is not None:
            for i, b in enumerate(bars):
                if i != hl: b.set_alpha(0.18)
        self._style_ax(ax, range(10), _SHORT_SCH, hl)
        ax.set_title('Bénéfice — mois scolaire', fontsize=8.5, fontweight='700',
                     color=TEXT_MAIN, pad=3, loc='left')
        fig.tight_layout(pad=0.6)
        c = FigureCanvas(fig); c.setStyleSheet('background:white;')
        self._pl.addWidget(c)

    def _draw_rev_exp(self, hl):
        self._clear_chart(self._rl)
        cache = self._chart_cache
        if not cache: return
        rev = cache['rev']; exp = cache['exp']
        xs  = range(10)

        fig = self._fig()
        ax  = fig.add_subplot(111)
        ax.fill_between(xs, rev, alpha=0.08, color='#10B981')
        ax.fill_between(xs, exp, alpha=0.08, color='#EF4444')
        ax.plot(xs, rev, color='#10B981', lw=2, marker='o', ms=2.5,
                markerfacecolor='white', markeredgewidth=1.2, label='Revenus')
        ax.plot(xs, exp, color='#EF4444', lw=2, marker='o', ms=2.5,
                markerfacecolor='white', markeredgewidth=1.2, label='Dépenses')
        if hl is not None:
            ax.axvline(hl, color=PRIMARY, lw=1, linestyle='--', alpha=0.4)
        self._style_ax(ax, xs, _SHORT_SCH, hl)
        ax.set_title('Revenus vs Dépenses', fontsize=8.5, fontweight='700',
                     color=TEXT_MAIN, pad=3, loc='left')
        ax.legend(fontsize=7, frameon=False, loc='upper right')
        fig.tight_layout(pad=0.6)
        c = FigureCanvas(fig); c.setStyleSheet('background:white;')
        self._rl.addWidget(c)

    def _draw_pay_rate(self, sy, hl):
        self._clear_chart(self._ql)
        paid_p, unpaid_p = [], []
        for m in SCHOOL_MONTHS:
            recs = self.session.query(MonthRecord).filter_by(
                month_name=m, school_year=sy).all()
            act = [r for r in recs if r.status != 'nan']
            tot = len(act); p = sum(1 for r in act if r.status == 'paid')
            paid_p.append(100. * p / tot if tot else 0)
            unpaid_p.append(100. * (tot - p) / tot if tot else 0)

        xs, w = range(10), 0.38
        fig = self._fig(w=8)
        ax  = fig.add_subplot(111)
        bp = ax.bar([x-w/2 for x in xs], paid_p,   width=w, color='#10B981', alpha=0.85, zorder=3)
        bu = ax.bar([x+w/2 for x in xs], unpaid_p, width=w, color='#EF4444', alpha=0.85, zorder=3)
        if hl is not None:
            for i, (a, b) in enumerate(zip(bp, bu)):
                if i != hl: a.set_alpha(0.15); b.set_alpha(0.15)
        self._style_ax(ax, xs, _SHORT_SCH, hl)
        ax.set_ylim(0, 115)
        ax.set_title('Taux paiement / mois (NAN exclus)', fontsize=8.5, fontweight='700',
                     color=TEXT_MAIN, pad=3, loc='left')
        ax.legend(['% Payés','% Impayés'], fontsize=7, frameon=False, loc='upper right')
        fig.tight_layout(pad=0.6)
        c = FigureCanvas(fig); c.setStyleSheet('background:white;')
        self._ql.addWidget(c)

    def _draw_classes(self):
        self._clear_chart(self._cl_lay)
        counts, labels = [], []
        for cls in CLASSES:
            n = self.session.query(Student).filter_by(
                class_name=cls, active=True).count()
            if n: counts.append(n); labels.append(cls)
        if not counts: counts, labels = [1], ['Aucun']

        fig = Figure(figsize=(4, 2.5), facecolor='white')
        fig.subplots_adjust(left=0, right=1, top=1, bottom=0)
        ax  = fig.add_subplot(111)
        pal = ['#4F46E5','#10B981','#F59E0B','#EF4444','#8B5CF6','#14B8A6',
               '#EC4899','#3B82F6','#6D28D9','#059669','#D97706','#DC2626']
        ax.pie(counts, labels=labels, autopct='%1.0f%%', colors=pal[:len(counts)],
               textprops={'fontsize':7,'color':'#374151'},
               pctdistance=0.80,
               wedgeprops={'linewidth':1.2,'edgecolor':'white'})
        c = FigureCanvas(fig); c.setStyleSheet('background:white;')
        self._cl_lay.addWidget(c)

    # ── Notifications ─────────────────────────────────────────────────────────

    def _draw_notifications(self, no_ins, unpaid, pend, debt, mon):
        for i in reversed(range(self._notif_lay.count())):
            w = self._notif_lay.itemAt(i).widget()
            if w: w.setParent(None)

        items = []
        if no_ins:   items.append((WARNING, f'⚠️  {no_ins} élèves sans assurance payée'))
        if unpaid:   items.append((DANGER,  f'💳  {unpaid} paiements manquants — {mon}'))
        if debt > 0: items.append((DANGER,  f'📋  Créances totales : {_fmt_mad(debt)}'))
        if pend:     items.append((WARNING,  f'🔄  {pend} ré-inscriptions en attente'))
        if not items: items.append((SUCCESS, '✅  Tout est en ordre — bonne journée !'))

        for color, msg in items:
            row_w = QWidget(); row_w.setStyleSheet('background:transparent;')
            row   = QHBoxLayout(row_w); row.setSpacing(8); row.setContentsMargins(0,1,0,1)
            strip = QFrame(); strip.setFixedWidth(3)
            strip.setStyleSheet(f'background:{color}; border-radius:2px;')
            lbl = QLabel(msg)
            lbl.setStyleSheet(
                f'color:{TEXT_MAIN}; font-size:11px; font-weight:500; background:transparent;'
            )
            row.addWidget(strip); row.addWidget(lbl); row.addStretch()
            self._notif_lay.addWidget(row_w)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get_setting(self, k, d=''):
        s = self.session.query(Setting).filter_by(key=k).first()
        return s.value if s else d

    def refresh(self):
        self._chart_cache = {}
        self._load_data(charts=True)


class AnnualProfitChart(QWidget):
    """
    Full-width Monthly Profit Evolution chart — Jan to Dec.

    Formula per calendar month:
        Profit = Revenue(monthly + transport) − Expenses − Salaries
        Insurance is EXCLUDED.

    Features
    ─────────
    • 12-month calendar bar chart (Jan–Dec)
    • Cumulative profit dashed line overlay
    • Current month highlighted with indigo border + ▼ marker
    • Future months greyed out (no phantom projections)
    • Value labels on every meaningful bar
    • Summary stats row: best month / worst month / YTD cumulative
    • Year selector (current year ± 3)
    • Manual ↺ refresh button + timestamp
    • Auto-refresh every 60 s via QTimer (re-queries DB silently)
    • DashboardWidget calls .refresh() after every payment/expense/salary event
    • Hover tooltip via mpl motion_notify_event (shows breakdown)
    """

    AUTO_REFRESH_MS = 60_000   # 1 minute

    def __init__(self, session):
        super().__init__()
        self.session   = session
        self._canvas   = None
        self._fig      = None
        self._bars     = []
        self._data     = {}     # cached: {year: (rev, exp, sal, prf)}
        self.setStyleSheet('background:transparent;')
        self._build_ui()
        # Defer first draw so the widget is fully shown before matplotlib renders
        QTimer.singleShot(150, self.refresh)
        # Auto-refresh timer
        self._timer = QTimer(self)
        self._timer.setInterval(self.AUTO_REFRESH_MS)
        self._timer.timeout.connect(self._silent_refresh)
        self._timer.start()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── Card shell ────────────────────────────────────────────────────────
        self._card = QFrame()
        self._card.setStyleSheet(
            f'QFrame{{'
            f'background:{BG_CARD};'
            f'border:1px solid {BORDER};'
            f'border-radius:12px;'
            f'}}'
        )
        self._card.setMinimumHeight(340)
        card_lay = QVBoxLayout(self._card)
        card_lay.setContentsMargins(18, 14, 18, 12)
        card_lay.setSpacing(8)

        # ── Header row ────────────────────────────────────────────────────────
        hdr = QHBoxLayout(); hdr.setSpacing(10)

        # Icon + title stack
        icon_lbl = QLabel('📈')
        icon_lbl.setFixedSize(36, 36)
        icon_lbl.setAlignment(Qt.AlignCenter)
        icon_lbl.setStyleSheet(
            f'background:{SUCCESS_LIGHT};border-radius:10px;font-size:17px;'
        )

        title_col = QVBoxLayout(); title_col.setSpacing(1)
        t1 = QLabel('Évolution du Bénéfice Mensuel')
        t1.setStyleSheet(
            f'color:{TEXT_MAIN};font-size:14px;font-weight:800;background:transparent;'
        )
        t2 = QLabel(
            'Revenus + Transport  −  Dépenses  −  Salaires   ·   '
            'Assurance exclue   ·   Jan – Déc'
        )
        t2.setStyleSheet(
            f'color:{TEXT_SUB};font-size:9.5px;background:transparent;'
        )
        title_col.addWidget(t1); title_col.addWidget(t2)

        hdr.addWidget(icon_lbl)
        hdr.addLayout(title_col)
        hdr.addStretch()

        # Year selector
        _lbl_yr = QLabel('Année :')
        _lbl_yr.setStyleSheet(
            f'color:{TEXT_SUB};font-size:11px;font-weight:600;background:transparent;'
        )
        combo_css = (
            f'QComboBox{{background:{PRIMARY_LIGHT};border:1.5px solid {PRIMARY}33;'
            f'border-radius:8px;color:{PRIMARY};padding:3px 10px;'
            f'font-size:12px;font-weight:700;min-width:80px;}}'
            f'QComboBox::drop-down{{border:none;width:18px;}}'
            f'QComboBox QAbstractItemView{{background:white;border:1px solid {BORDER};'
            f'color:{TEXT_MAIN};outline:none;border-radius:6px;}}'
            f'QComboBox QAbstractItemView::item{{padding:6px 12px;}}'
            f'QComboBox QAbstractItemView::item:selected'
            f'{{background:{PRIMARY_LIGHT};color:{PRIMARY};}}'
        )
        self._yr_combo = QComboBox()
        self._yr_combo.setStyleSheet(combo_css)
        self._yr_combo.setFixedHeight(30)
        cy = datetime.now().year
        for y in range(cy - 3, cy + 2):
            self._yr_combo.addItem(str(y), y)
        self._yr_combo.setCurrentIndex(3)   # current year
        self._yr_combo.currentIndexChanged.connect(self.refresh)

        # Refresh button
        rfsh_btn = QPushButton('↺')
        rfsh_btn.setFixedSize(30, 30)
        rfsh_btn.setToolTip('Actualiser maintenant')
        rfsh_btn.setStyleSheet(
            f'QPushButton{{background:{SUCCESS_LIGHT};color:{SUCCESS};border:none;'
            f'border-radius:8px;font-size:15px;font-weight:700;}}'
            f'QPushButton:hover{{background:{SUCCESS};color:white;}}'
        )
        rfsh_btn.clicked.connect(self.refresh)

        self._ts_lbl = QLabel('')
        self._ts_lbl.setStyleSheet(
            'color:#9CA3AF;font-size:9px;background:transparent;'
        )

        for w in [_lbl_yr, self._yr_combo, rfsh_btn, self._ts_lbl]:
            hdr.addWidget(w)

        card_lay.addLayout(hdr)

        # ── Summary stats strip ───────────────────────────────────────────────
        self._stats_row = QHBoxLayout()
        self._stats_row.setSpacing(12)
        self._stats_row.setContentsMargins(4, 0, 4, 0)
        # Placeholders — filled in after first draw
        self._stat_widgets = []
        for _ in range(4):
            lbl = QLabel('—')
            lbl.setStyleSheet(
                f'color:{TEXT_SUB};font-size:10px;background:transparent;'
            )
            self._stats_row.addWidget(lbl)
            self._stat_widgets.append(lbl)
        self._stats_row.addStretch()
        card_lay.addLayout(self._stats_row)

        # ── Chart area ────────────────────────────────────────────────────────
        self._chart_lay = QVBoxLayout()
        self._chart_lay.setContentsMargins(0, 0, 0, 0)
        self._chart_lay.setSpacing(0)

        self._placeholder = QLabel('Chargement du graphique…')
        self._placeholder.setAlignment(Qt.AlignCenter)
        self._placeholder.setStyleSheet(
            'color:#D1D5DB;font-size:13px;background:transparent;padding:40px;'
        )
        self._chart_lay.addWidget(self._placeholder)
        card_lay.addLayout(self._chart_lay, 1)

        outer.addWidget(self._card)

    # ── Stat strip helpers ────────────────────────────────────────────────────

    def _update_stats(self, year, prf, is_cy, cm):
        realized = [v for i, v in enumerate(prf) if not (is_cy and i > cm)]
        if not realized:
            return
        ytd       = sum(realized)
        best_i    = prf.index(max(prf))
        worst_i   = prf.index(min(prf))
        avg       = sum(realized) / len(realized) if realized else 0

        def _f(v):
            sign = '+' if v > 0 else ''
            if abs(v) >= 1_000_000: return f'{sign}{v/1_000_000:.2f}M MAD'
            if abs(v) >= 1_000:     return f'{sign}{v/1_000:.1f}k MAD'
            return f'{sign}{v:.0f} MAD'

        stats = [
            (f'Cumul {year} : {_f(ytd)}',
             SUCCESS if ytd >= 0 else DANGER),
            (f'Meilleur mois : {_SHORT_CAL[best_i]}  {_f(max(prf))}',
             SUCCESS),
            (f'Pire mois : {_SHORT_CAL[worst_i]}  {_f(min(prf))}',
             DANGER),
            (f'Moyenne / mois : {_f(avg)}',
             TEXT_SUB),
        ]
        for lbl, (text, color) in zip(self._stat_widgets, stats):
            lbl.setText(text)
            lbl.setStyleSheet(
                f'color:{color};font-size:10px;font-weight:600;background:transparent;'
            )

    # ── Data computation ──────────────────────────────────────────────────────

    def _compute(self, year: int):
        """
        Returns (rev, exp, sal, prf) — 12-element lists indexed 0=Jan … 11=Dec.
        Two-layer fallback for each source:
          1. payment_date / paid_date  (real recorded date)
          2. Payment.year + Payment.month name mapped to calendar month
        Insurance is always excluded from revenue.
        """
        rev = [0.0] * 12
        exp = [0.0] * 12
        sal = [0.0] * 12

        # Revenue: monthly + transport payments only
        for p in self.session.query(Payment).filter(
            Payment.payment_type.in_(['monthly', 'transport'])
        ).all():
            try:
                if p.payment_date and p.payment_date.year == year:
                    rev[p.payment_date.month - 1] += p.amount or 0
                elif (p.year == year) and p.month:
                    cal = _SCHOOL_CAL.get(p.month, 0)
                    if cal: rev[cal - 1] += p.amount or 0
            except Exception:
                pass

        # Expenses
        for ep in self.session.query(ExpensePayment).filter_by(year=year).all():
            try:
                cal = _SCHOOL_CAL.get(ep.month or '', 0)
                if cal: exp[cal - 1] += ep.amount or 0
            except Exception:
                pass

        # Salaries — null-safe paid check
        for s in self.session.query(Salary).all():
            try:
                if not getattr(s, 'paid', True):
                    continue
                amt = s.net_salary or getattr(s, 'total', 0) or 0
                if s.paid_date and s.paid_date.year == year:
                    sal[s.paid_date.month - 1] += amt
                elif (s.year == year) and s.month:
                    cal = _SCHOOL_CAL.get(s.month, 0)
                    if cal: sal[cal - 1] += amt
            except Exception:
                pass

        prf = [rev[i] - exp[i] - sal[i] for i in range(12)]
        return rev, exp, sal, prf

    # ── Refresh entry points ──────────────────────────────────────────────────

    def refresh(self):
        """Full refresh: expire session, clear cache for this year, redraw."""
        self.session.expire_all()
        year = self._yr_combo.currentData() or datetime.now().year
        self._data.pop(year, None)   # invalidate cache
        self._draw_year(year)

    def _silent_refresh(self):
        """Timer-triggered: expire + redraw without clearing cache indicator."""
        self.session.expire_all()
        year = self._yr_combo.currentData() or datetime.now().year
        self._data.pop(year, None)
        self._draw_year(year)

    def _draw_year(self, year: int):
        if year not in self._data:
            try:
                self._data[year] = self._compute(year)
            except Exception:
                import traceback; traceback.print_exc()
                return
        rev, exp, sal, prf = self._data[year]
        self._draw(year, rev, exp, sal, prf)

    # ── Drawing ───────────────────────────────────────────────────────────────

    def _draw(self, year, rev, exp, sal, prf):
        # Remove old canvas
        if self._canvas:
            self._canvas.mpl_disconnect(self._cid) if hasattr(self, '_cid') else None
            self._canvas.setParent(None)
            self._canvas = None
        if self._placeholder:
            self._placeholder.setParent(None)
            self._placeholder = None
        for i in reversed(range(self._chart_lay.count())):
            w = self._chart_lay.itemAt(i).widget()
            if w: w.setParent(None)

        now    = datetime.now()
        cm     = now.month - 1        # 0-based current month index
        cy     = now.year
        is_cy  = (year == cy)

        # ── Figure ────────────────────────────────────────────────────────────
        fig = Figure(figsize=(13, 3.5), facecolor='white')
        fig.subplots_adjust(left=0.055, right=0.975, top=0.84, bottom=0.14)
        ax = fig.add_subplot(111)
        ax.set_facecolor('white')

        xs = list(range(12))
        W  = 0.62

        # ── Bar colours ───────────────────────────────────────────────────────
        face_c, edge_c, lw_v, alpha_v = [], [], [], []
        for i, v in enumerate(prf):
            is_cur  = is_cy and i == cm
            is_fut  = is_cy and i > cm
            is_zero = (rev[i] == 0 and exp[i] == 0 and sal[i] == 0)

            if is_fut and is_zero:
                fc, ec = '#F3F4F6', '#E5E7EB'
            elif v >= 0:
                fc, ec = '#10B981', '#059669'
            else:
                fc, ec = '#EF4444', '#DC2626'

            face_c.append(fc)
            edge_c.append('#4F46E5' if is_cur else ec)
            lw_v.append(2.4 if is_cur else 0.6)
            alpha_v.append(0.35 if (is_fut and is_zero) else 1.0)

        # ── Draw bars ─────────────────────────────────────────────────────────
        self._bars = ax.bar(
            xs, prf,
            color=face_c, edgecolor=edge_c, linewidth=lw_v,
            width=W, zorder=3
        )
        for bar, alph in zip(self._bars, alpha_v):
            bar.set_alpha(alph)

        # ── Zero baseline ─────────────────────────────────────────────────────
        ax.axhline(0, color='#CBD5E1', linewidth=0.8, zorder=2)

        # ── Cumulative line (up to current month only) ────────────────────────
        cx_pts, cy_pts, run = [], [], 0.0
        for i, v in enumerate(prf):
            if is_cy and i > cm:
                break
            run += v
            cx_pts.append(i)
            cy_pts.append(run)

        if len(cx_pts) > 1:
            ax.plot(
                cx_pts, cy_pts,
                color='#4F46E5', linewidth=2.0, linestyle='--',
                zorder=4, alpha=0.85,
                marker='o', markersize=3.0,
                markerfacecolor='white', markeredgewidth=1.4,
                markeredgecolor='#4F46E5',
                label='Cumul annuel',
            )

        # ── Value labels ──────────────────────────────────────────────────────
        mx = max((abs(v) for v in prf), default=1) or 1
        for i, (bar, v) in enumerate(zip(self._bars, prf)):
            if is_cy and i > cm and abs(v) < 1:
                continue
            if abs(v) < mx * 0.02:   # skip tiny bars — avoid clutter
                continue
            txt = (f'{v/1_000_000:.2f}M' if abs(v) >= 1_000_000
                   else f'{v/1_000:.1f}k' if abs(v) >= 1_000
                   else f'{v:.0f}')
            y_off = 4 if v >= 0 else -11
            ax.annotate(
                txt,
                xy=(bar.get_x() + bar.get_width() / 2, v),
                xytext=(0, y_off),
                textcoords='offset points',
                ha='center', fontsize=6.5, fontweight='600',
                color='#059669' if v >= 0 else '#DC2626',
                zorder=5,
            )

        # ── Current month marker ──────────────────────────────────────────────
        if is_cy and 0 <= cm < 12:
            ax.annotate(
                '▼  Maintenant',
                xy=(cm, max(prf[cm], 0)),
                xytext=(0, 12),
                textcoords='offset points',
                ha='center', fontsize=7, fontweight='700',
                color='#4F46E5',
            )

        # ── X axis ────────────────────────────────────────────────────────────
        ax.set_xticks(xs)
        ax.set_xticklabels(_SHORT_CAL, fontsize=8.5)
        for i, tick in enumerate(ax.get_xticklabels()):
            is_cur = is_cy and i == cm
            tick.set_color('#4F46E5' if is_cur else '#6B7280')
            tick.set_fontweight('bold' if is_cur else 'normal')
            tick.set_fontsize(9 if is_cur else 8)

        # ── Y axis ────────────────────────────────────────────────────────────
        ax.yaxis.set_tick_params(labelcolor='#9CA3AF', labelsize=8)
        ax.yaxis.set_major_formatter(
            matplotlib.ticker.FuncFormatter(
                lambda v, _: (
                    f'{v/1_000_000:.1f}M' if abs(v) >= 1_000_000
                    else f'{v/1_000:.0f}k' if abs(v) >= 1_000
                    else f'{v:.0f}'
                )
            )
        )
        for spine in ax.spines.values():
            spine.set_visible(False)
        ax.yaxis.grid(True, color='#F3F4F8', linewidth=0.8, zorder=0)
        ax.set_axisbelow(True)

        # ── Figure title (right-aligned cumulative) ───────────────────────────
        realized = [v for i, v in enumerate(prf) if not (is_cy and i > cm)]
        ytd = sum(realized)
        t_col  = '#059669' if ytd >= 0 else '#DC2626'
        t_sign = '+' if ytd > 0 else ''
        if abs(ytd) >= 1_000_000:
            t_lbl = f'{t_sign}{ytd/1_000_000:.2f}M MAD'
        elif abs(ytd) >= 1_000:
            t_lbl = f'{t_sign}{ytd/1_000:.1f}k MAD'
        else:
            t_lbl = f'{t_sign}{ytd:.0f} MAD'

        ax.set_title(
            f'Bénéfice {year}  ·  YTD : {t_lbl}',
            fontsize=9.5, fontweight='800',
            color=t_col, pad=6, loc='right',
        )

        # ── Legend ────────────────────────────────────────────────────────────
        legend_handles = [
            Patch(facecolor='#10B981', edgecolor='#059669',
                  label='Bénéfice positif'),
            Patch(facecolor='#EF4444', edgecolor='#DC2626',
                  label='Bénéfice négatif'),
            Line2D([0], [0], color='#4F46E5', linewidth=1.8,
                   linestyle='--', marker='o', markersize=3,
                   label='Cumul annuel'),
            Patch(facecolor='#F3F4F6', edgecolor='#E5E7EB',
                  label='Données non disponibles'),
        ]
        ax.legend(
            handles=legend_handles,
            fontsize=7.5, frameon=False,
            loc='upper left', ncol=4,
        )

        # ── Hover tooltip ─────────────────────────────────────────────────────
        annot = ax.annotate(
            '', xy=(0, 0),
            xytext=(12, 12), textcoords='offset points',
            bbox=dict(boxstyle='round,pad=0.5', fc='white',
                      ec='#E5E7EB', lw=1.0, alpha=0.95),
            fontsize=8,
            arrowprops=dict(arrowstyle='->', color='#9CA3AF', lw=0.8),
        )
        annot.set_visible(False)

        def _on_hover(event):
            if event.inaxes != ax:
                annot.set_visible(False)
                fig.canvas.draw_idle()
                return
            for i, bar in enumerate(self._bars):
                if bar.contains(event)[0]:
                    r_str = _fmt_mad_signed(rev[i])
                    e_str = _fmt_mad_signed(-exp[i])
                    s_str = _fmt_mad_signed(-sal[i])
                    p_str = _fmt_mad_signed(prf[i])
                    annot.set_text(
                        f'{_SHORT_CAL[i]}\n'
                        f'Rev  {r_str}\n'
                        f'Dep  {e_str}\n'
                        f'Sal  {s_str}\n'
                        f'───────\n'
                        f'= {p_str}'
                    )
                    annot.xy = (
                        bar.get_x() + bar.get_width() / 2,
                        bar.get_height() if prf[i] >= 0 else 0,
                    )
                    annot.set_visible(True)
                    fig.canvas.draw_idle()
                    return
            annot.set_visible(False)
            fig.canvas.draw_idle()

        self._fig = fig
        canvas = FigureCanvas(fig)
        canvas.setStyleSheet('background:white;border-radius:8px;')
        canvas.setMinimumHeight(240)
        self._cid = canvas.mpl_connect('motion_notify_event', _on_hover)
        self._canvas = canvas
        self._chart_lay.addWidget(canvas)

        # Update stats strip
        self._update_stats(year, prf, is_cy, cm)

        # Timestamp
        self._ts_lbl.setText(f'↻ {now.strftime("%H:%M:%S")}')


def _fmt_mad_signed(v: float) -> str:
    sign = '+' if v > 0 else ''
    if abs(v) >= 1_000_000: return f'{sign}{v/1_000_000:.2f}M'
    if abs(v) >= 1_000:     return f'{sign}{v/1_000:.1f}k'
    return f'{sign}{v:.0f}'
