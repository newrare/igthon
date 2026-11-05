"""Tests for the instrument exposure reading (src/core/exposure.py)."""

from src.core.exposure import exposure_of


class TestExposureOf:
    def test_currency_pair_carries_both_currencies(self):
        exposure = exposure_of("CS.D.EURJPY.CFD.IP")

        assert exposure.asset_class == "forex"
        assert exposure.tags == {"ccy:EUR", "ccy:JPY"}

    def test_pairs_sharing_a_currency_are_one_bet(self):
        assert exposure_of("CS.D.EURJPY.CFD.IP").shares_exposure(
            exposure_of("CS.D.EURGBP.CFD.IP")
        )
        assert not exposure_of("CS.D.EURJPY.CFD.IP").shares_exposure(
            exposure_of("CS.D.GBPUSD.CFD.IP")
        )

    def test_indices_of_one_region_are_one_bet(self):
        dax, cac, topix = (
            exposure_of(epic)
            for epic in ("IX.D.DAX.IFMM.IP", "IX.D.CAC.IDF.IP", "IX.D.TOPIX.IFU.IP")
        )

        assert dax.asset_class == "index"
        assert dax.shares_exposure(cac)
        assert not dax.shares_exposure(topix)

    def test_spot_and_future_on_one_metal_are_one_bet(self):
        spot = exposure_of("CS.D.CFDSILVER.CFDSI.IP")
        future = exposure_of("MT.D.SI.FWM1.IP")

        assert spot.asset_class == future.asset_class == "metal"
        assert spot.shares_exposure(future)
        assert exposure_of("CS.D.CFEGOLD.CFE.IP").asset_class == "metal"

    def test_commodity_classes(self):
        assert exposure_of("CC.D.CL.UEF.IP").asset_class == "energy"
        assert exposure_of("EN.D.ICENG.FWS0.IP").asset_class == "energy"
        assert exposure_of("CO.D.BO.FWM4.IP").asset_class == "agri"
        assert exposure_of("CC.D.W.UNC.IP").asset_class == "agri"

    def test_unknown_instrument_is_its_own_bet(self):
        a = exposure_of("XX.D.FOO.IP")
        b = exposure_of("XX.D.BAR.IP")

        assert a.asset_class == "XX.D"
        assert not a.shares_exposure(b)
