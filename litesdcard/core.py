# This file is Copyright (c) 2017-2020 Florent Kermarrec <florent@enjoy-digital.fr>
# This file is Copyright (c) 2017 Pierre-Olivier Vauboin <po@lambdaconcept.com>
# This file is Copyright (c) 2018 bunnie <bunnie@kosagi.com>
# License: BSD

from migen import *
from migen.genlib.cdc import MultiReg

from litex.soc.interconnect.csr import *
from litex.soc.interconnect import stream

from litesdcard.common import *
from litesdcard.crc import CRC, CRCChecker
from litesdcard.crc import CRCDownstreamChecker, CRCUpstreamInserter

# SDCore -------------------------------------------------------------------------------------------

class SDCore(Module, AutoCSR):
    def __init__(self, phy):
        self.sink   = stream.Endpoint([("data", 32)])
        self.source = stream.Endpoint([("data", 32)])

        self.argument   = CSRStorage(32)
        self.command    = CSRStorage(32)
        self.send       = CSR()

        self.response   = CSRStatus(128)

        self.cmdevt     = CSRStatus(4)
        self.dataevt    = CSRStatus(4)

        self.blocksize  = CSRStorage(10)
        self.blockcount = CSRStorage(32)

        # # #

        argument    = self.argument.storage
        command     = self.command.storage
        response    = Signal(136)
        cmdevt      = Signal(4)
        dataevt     = Signal(4)
        blocksize   = self.blocksize.storage
        blockcount  = self.blockcount.storage

        self.comb += [
            self.response.status.eq(response),
            self.cmdevt.status.eq(cmdevt),
            self.dataevt.status.eq(dataevt),
        ]

        self.submodules.crc7_inserter  = crc7_inserter  = CRC(9, 7, 40)
        self.submodules.crc16_inserter = crc16_inserter = CRCUpstreamInserter()
        self.submodules.crc16_checker  = crc16_checker  = CRCDownstreamChecker()

        self.submodules.upstream_converter   = stream.StrideConverter([('data', 32)], [('data', 8)], reverse=True)
        self.submodules.downstream_converter = stream.StrideConverter([('data', 8)], [('data', 32)], reverse=True)

        self.comb += [
            self.sink.connect(self.upstream_converter.sink),
            self.upstream_converter.source.connect(crc16_inserter.sink),

            crc16_checker.source.connect(self.downstream_converter.sink),
            self.downstream_converter.source.connect(self.source),
        ]

        cmd_type    = Signal(2)
        cmd_count   = Signal(3)
        cmd_done    = Signal()
        cmd_error   = Signal()
        cmd_timeout = Signal()

        data_type    = Signal(2)
        data_count   = Signal(32)
        data_done    = Signal()
        data_error   = Signal()
        data_timeout = Signal()

        self.comb += [
            cmd_type.eq(command[0:2]),
            data_type.eq(command[5:7]),
            cmdevt.eq(Cat(
                cmd_done,
                cmd_error,
                cmd_timeout,
                0)), # FIXME cmd response CRC.
            dataevt.eq(Cat(
                data_done,
                data_error,
                data_timeout,
                ~crc16_checker.valid)),
            crc7_inserter.val.eq(Cat(
                argument,
                command[8:14],
                1,
                0)),
            crc7_inserter.clr.eq(1),
            crc7_inserter.enable.eq(1),
        ]

        self.submodules.fsm = fsm = FSM()
        fsm.act("IDLE",
            NextValue(cmd_done,   1),
            NextValue(data_done,  1),
            NextValue(cmd_count,  0),
            NextValue(data_count, 0),
            If(self.send.re,
                NextValue(cmd_done,     0),
                NextValue(cmd_error,    0),
                NextValue(cmd_timeout,  0),
                NextValue(data_done,    0),
                NextValue(data_error,   0),
                NextValue(data_timeout, 0),
                NextState("CMD")
            )
        )
        fsm.act("CMD",
            phy.cmdw.sink.valid.eq(1),
            Case(cmd_count, {
                0: phy.cmdw.sink.data.eq(Cat(command[8:14], 1, 0)),
                1: phy.cmdw.sink.data.eq(argument[24:32]),
                2: phy.cmdw.sink.data.eq(argument[16:24]),
                3: phy.cmdw.sink.data.eq(argument[ 8:16]),
                4: phy.cmdw.sink.data.eq(argument[ 0: 8]),
                5: [phy.cmdw.sink.data.eq(Cat(1, crc7_inserter.crc)),
                    phy.cmdw.sink.last.eq(cmd_type == SDCARD_CTRL_RESPONSE_NONE)]
               }
            ),
            If(phy.cmdw.sink.valid & phy.cmdw.sink.ready,
                NextValue(cmd_count, cmd_count + 1),
                If(cmd_count == (6-1),
                    If(cmd_type == SDCARD_CTRL_RESPONSE_NONE,
                        NextValue(cmd_done, 1),
                        NextState("IDLE")
                    ).Else(
                        NextState("CMD-RESPONSE-0"),
                    )
                )
            )
        )
        fsm.act("CMD-RESPONSE-0",
            phy.cmdr.sink.valid.eq(1),
            phy.cmdr.sink.last.eq(data_type == SDCARD_CTRL_DATA_TRANSFER_NONE),
            If(cmd_type == SDCARD_CTRL_RESPONSE_LONG,
                phy.cmdr.sink.length.eq(17) # 136bits
            ).Else(
                phy.cmdr.sink.length.eq(6)  # 48bits
            ),
            If(phy.cmdr.sink.valid & phy.cmdr.sink.ready,
                NextState("CMD-RESPONSE-1")
            )
        )
        fsm.act("CMD-RESPONSE-1",
            phy.cmdr.source.ready.eq(1),
            If(phy.cmdr.source.valid,
                If(phy.cmdr.source.status == SDCARD_STREAM_STATUS_TIMEOUT,
                    NextValue(cmd_timeout, 1),
                    NextState("IDLE")
                ).Elif(phy.cmdr.source.last,
                    If(data_type == SDCARD_CTRL_DATA_TRANSFER_WRITE,
                        NextState("DATA-WRITE")
                    ).Elif(data_type == SDCARD_CTRL_DATA_TRANSFER_READ,
                        NextState("DATA-READ-0")
                    ).Else(
                        NextState("IDLE")
                    ),
                ).Else(
                    NextValue(response, Cat(phy.cmdr.source.data, response))
                )
            )
        )
        fsm.act("DATA-WRITE",
            crc16_inserter.source.connect(phy.dataw.sink),
            If(phy.dataw.sink.valid &
                phy.dataw.sink.last &
                phy.dataw.sink.ready,
                NextValue(data_count, data_count + 1),
                If(data_count == (blockcount-1),
                    NextState("IDLE")
                )
            ),
            phy.datar.source.ready.eq(1),
            If(phy.datar.source.valid,
                If(phy.datar.source.status != SDCARD_STREAM_STATUS_DATAACCEPTED,
                    NextValue(data_error, 1)
                )
            )
        )
        fsm.act("DATA-READ-0",
            phy.datar.sink.valid.eq(1),
            phy.datar.sink.blocksize.eq(blocksize),
            phy.datar.sink.last.eq(data_count == (blockcount - 1)),
            If(phy.datar.sink.valid & phy.datar.sink.ready,
                NextState("DATA-READ-1")
            ),
        )
        fsm.act("DATA-READ-1",
            phy.datar.source.ready.eq(1),
            If(phy.datar.source.valid,
                If(phy.datar.source.status == SDCARD_STREAM_STATUS_OK,
                    phy.datar.source.connect(crc16_checker.sink, omit={"status"}),
                    If(phy.datar.source.last & phy.datar.source.ready,
                        NextValue(data_count, data_count + 1),
                        If(data_count == (blockcount - 1),
                            NextState("IDLE")
                        ).Else(
                            NextState("DATA-READ-0")
                        )
                    )
                ).Elif(phy.datar.source.status == SDCARD_STREAM_STATUS_TIMEOUT,
                    NextValue(data_timeout, 1),
                    NextValue(data_count, 0),
                    phy.datar.source.ready.eq(1),
                    NextState("IDLE")
                )
            )
        )
