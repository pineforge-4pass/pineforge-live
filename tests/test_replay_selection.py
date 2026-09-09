import pytest
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from pineforge_live import types as T
from pineforge_live.bars.calendar import ParentWindows
from pineforge_live.verification.probe_case import replay_calendar, choose_window
from pineforge_live.verification.selection import select_probes


def packet():
 return {'probes':[{'probe_id':str(i),'lane':'a' if i<4 else 'b','group':'standard'} for i in range(8)]}


def test_selection_is_stable_stratified_and_not_outcome_based():
 p=packet();a=select_probes(p);p['probes'].reverse();b=select_probes(p)
 assert a==b and len(a)==4
 assert sum(x['lane']=='a' for x in a)==2
 assert select_probes(p,all_probes=True)==sorted(p['probes'],key=lambda x:x['probe_id'])


def test_explicit_identity_and_missing_selection():
 p=packet();assert [x['probe_id'] for x in select_probes(p,probe_ids=['3','1'])]==['1','3']
 with pytest.raises(ValueError):select_probes(p,probe_ids=['missing'])
 p['probes'].append(p['probes'][0])
 with pytest.raises(ValueError):select_probes(p)


def bar(stamp):
 return T.NormalizedBar(stamp,100.,101.,99.,100.,1.,0)


def ms(text,zone='UTC'):
 return int(datetime.fromisoformat(text).replace(tzinfo=ZoneInfo(zone)).timestamp())*1000


@pytest.mark.parametrize('gap_policy',['reject','observed'])
def test_missing_intraday_closing_rows_cannot_shorten_windows(gap_policy):
 chart=[bar(i*180000) for i in range(4)]
 complete=list(range(0,4*180000,60000))
 missing_closes=[stamp for stamp in complete if stamp%180000!=120000]
 calendar=replay_calendar(chart,missing_closes,'3')
 assert calendar==replay_calendar(chart,complete,'3')
 assert calendar==[(0,180000),(180000,360000),(360000,540000),(540000,720000)]
 assert all(row['close_known'] for row in calendar.provenance)
 assert (calendar[1][1]-calendar[1][0])//60000-2==1
 with pytest.raises(ValueError,match='no complete'):
  choose_window(chart,missing_closes,calendar,[],1,gap_policy=gap_policy)


def test_intraday_close_can_use_an_independent_earlier_native_open():
 chart=[bar(0),bar(120000),bar(300000)]
 calendar=replay_calendar(chart,[0,120000,300000],'3')
 assert calendar==[(0,120000),(120000,300000),(300000,480000)]
 assert all(row['close_source']=='native-next-open/script-timeframe' for row in calendar.provenance)


def test_declared_overnight_daily_close_preserves_dst_and_missing_tail():
 zone='America/New_York'
 chart=[bar(ms(f'2026-03-{day:02d}T17:00',zone)) for day in range(6,10)]
 expected=[(b.ts_open,ms(f'2026-03-{day+1:02d}T17:00',zone),ms(f'2026-03-{day:02d}T18:00',zone))
           for day,b in zip(range(6,10),chart)]
 complete=[t for _,end,first in expected for t in range(first,end,60000)]
 missing_tail=[t for t in complete if all(t<end-120000 or t>=end for _,end,_ in expected)]
 calendar=replay_calendar(chart,missing_tail,'1D',session='1800-1700',timezone=zone)
 assert calendar==expected
 assert calendar==replay_calendar(chart,complete,'1D',session='1800-1700',timezone=zone)
 assert (calendar[1][1]-calendar[1][2])//60000==22*60
 with pytest.raises(ValueError,match='no complete'):
  choose_window(chart,missing_tail,calendar,[],1,gap_policy='observed')
 assert choose_window(chart,complete,calendar,[],1,gap_policy='observed')==(1,2)


def test_regular_daytime_close_is_session_end_not_last_quote():
 zone='Asia/Kolkata'
 chart=[bar(ms(f'2026-03-{day:02d}T09:15',zone)) for day in range(2,6)]
 short_quotes=[b.ts_open+offset*60000 for b in chart for offset in range(10)]
 calendar=replay_calendar(chart,short_quotes,'1D',session='0915-1530',timezone=zone)
 assert [row[1] for row in calendar]==[ms(f'2026-03-{day:02d}T15:30',zone) for day in range(2,6)]
 with pytest.raises(ValueError,match='no complete'):
  choose_window(chart,short_quotes,calendar,[],1,gap_policy='observed')


def test_unknown_special_close_is_excluded_but_native_warmup_is_retained():
 zone='Asia/Kolkata'
 chart=[bar(ms(f'2021-11-{day:02d}T{clock}',zone))
        for day,clock in [(3,'09:15'),(4,'18:00'),(8,'09:15'),(9,'09:15')]]
 stamps=[b.ts_open+offset*60000 for i,b in enumerate(chart)
         for offset in range(7 if i==1 else 0,60 if i==1 else 375)]
 calendar=replay_calendar(chart,stamps,'1D',session='0915-1530',timezone=zone)
 assert calendar.unknown_close_indices==frozenset({1})
 detail=calendar.provenance[1]
 assert detail['close_ms'] is None and not detail['replay_eligible']
 assert detail['close_source']=='unknown-special-session'
 assert calendar[1][0]==chart[1].ts_open
 assert calendar[1][1]==chart[1].ts_open+86400000  # Warmup bound, never a claimed close.
 ParentWindows(calendar).validate_prefix(chart)
 assert calendar.provenance==replay_calendar(chart,stamps[::2],'1D',session='0915-1530',timezone=zone).provenance
 trades=[SimpleNamespace(exit_bar_index=1,open_at_end=False)]
 assert choose_window(chart,stamps,calendar,trades,1,gap_policy='observed')==(2,3)


def test_24x7_daily_close_is_independent_of_supplied_rows():
 chart=[bar(ms(f'2026-03-{day:02d}T00:00')) for day in range(2,6)]
 calendar=replay_calendar(chart,[b.ts_open for b in chart],'1D')
 assert calendar==[(b.ts_open,b.ts_open+86400000) for b in chart]
 assert not calendar.unknown_close_indices


@pytest.mark.parametrize('text',['2026-03-08T00:00','2026-11-01T00:00'])
def test_24x7_daily_close_is_fixed_duration_across_exchange_dst(text):
 first=ms(text,'America/New_York')
 chart=[bar(first+i*86400000) for i in range(4)]
 calendar=replay_calendar(chart,[],'1D',timezone='America/New_York')
 assert calendar==[(b.ts_open,b.ts_open+86400000) for b in chart]
 assert not calendar.unknown_close_indices


def test_weekly_windows_are_not_given_a_single_daily_session_close():
 chart=[bar(i*7*86400000) for i in range(4)]
 calendar=replay_calendar(chart,[],'1W',session='0930-1600',timezone='America/New_York')
 assert calendar.unknown_close_indices==frozenset(range(4))
 assert all(row['close_ms'] is None and row['close_source']=='unknown-calendar-timeframe'
            for row in calendar.provenance)
 with pytest.raises(ValueError,match='no complete'):
  choose_window(chart,[],calendar,[],1,gap_policy='observed')
