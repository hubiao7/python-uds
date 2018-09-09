#!/usr/bin/env python

__author__ = "Richard Clubb"
__copyrights__ = "Copyright 2018, the python-uds project"
__credits__ = ["Richard Clubb"]

__license__ = "MIT"
__maintainer__ = "Richard Clubb"
__email__ = "richard.clubb@embeduk.com"
__status__ = "Development"


import can
from iTp import iTp
from Utilities.ResettableTimer import ResettableTimer
from time import perf_counter
from struct import unpack
from CanTpTypes import CanTpAddressingTypes, CanTpState, CanTpMessageType, CanTpFsTypes
from CanTpTypes import CANTP_MAX_PAYLOAD_LENGTH, SINGLE_FRAME_DL_INDEX, FIRST_FRAME_DL_INDEX_HIGH, \
    FIRST_FRAME_DL_INDEX_LOW, FC_BS_INDEX, FC_STMIN_INDEX, N_PCI_INDEX, FIRST_FRAME_DATA_START_INDEX, \
    SINGLE_FRAME_DATA_START_INDEX, CONSECUTIVE_FRAME_SEQUENCE_NUMBER_INDEX, \
    CONSECUTIVE_FRAME_SEQUENCE_DATA_START_INDEX, FLOW_CONTROL_BS_INDEX, FLOW_CONTROL_STMIN_INDEX


def fillArray(data, length, fillValue=0):
    output = []
    for i in range(0, length):
        output.append(fillValue)
    for i in range(0, len(data)):
        output[i] = data[i]
    return output


##
# @class CanTp
# @brief This is the main class to support CAN transport protocol
#
# Will spawn a CanTpListener class for incoming messages
# depends on a bus object for communication on CAN
class CanTp(iTp):

    ##
    # @brief constructor for the CanTp object
    def __init__(self, reqId=None, resId=None):

        self.__bus = self.createBusConnection()

        # there probably needs to be an adapter to deal with these parts as they couple to python-can heavily
        self.__listener = can.Listener()
        self.__listener.on_message_received = self.callback_onReceive
        self.__notifier = can.Notifier(self.__bus, [self.__listener], 0)

        self.__reqId = reqId
        self.__resId = resId

        self.__recvBuffer = []

        # this needs expanding to support the other addressing types
        self.__addressingType = CanTpAddressingTypes.NORMAL_FIXED

        if(self.__addressingType == CanTpAddressingTypes.NORMAL_FIXED):
            self.__maxPduLength = 7
            self.__pduStartIndex = 0
        else:
            self.__maxPduLength = 6
            self.__pduStartIndex = 1



    ##
    # @brief connection method
    def createBusConnection(self):
        # check config file and load
        bus = can.interface.Bus('test', bustype='virtual')
        #bus = pcan.PcanBus('PCAN_USBBUS1')
        return bus

    ##
    # @brief send method
    # @param [in] payload the payload to be sent
    def send(self, payload):

        payloadLength = len(payload)
        payloadPtr = 0

        state = CanTpState.IDLE

        if payloadLength > CANTP_MAX_PAYLOAD_LENGTH:
            raise Exception("Payload too large for CAN Transport Protocol")

        if payloadLength < self.__maxPduLength:
            state = CanTpState.SEND_SINGLE_FRAME
        else:
            state = CanTpState.SEND_FIRST_FRAME

        txPdu = [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]

        sequenceNumber = 0
        endOfBlock_flag = False
        endOfMessage_flag = False

        blockList = []
        currBlock = []

        timeoutTimer = ResettableTimer(100)
        stMinTimer = ResettableTimer()

        self.clearBufferedMessages()

        cfTiming = []

        while endOfMessage_flag is False:

            recvPdu = self.getNextBufferedMessage()

            if recvPdu is not None:
                N_PCI = (recvPdu[0] & 0xF0) >> 4
                if N_PCI == CanTpMessageType.FLOW_CONTROL:
                    fs = recvPdu[0] & 0x0F
                    if fs == CanTpFsTypes.WAIT:
                        raise Exception("Wait not currently supported")
                    elif fs == CanTpFsTypes.OVERFLOW:
                        raise Exception("Overflow received from ECU")
                    elif fs == CanTpFsTypes.CONTINUE_TO_SEND:
                        if state == CanTpState.WAIT_FLOW_CONTROL:
                            if fs == CanTpFsTypes.CONTINUE_TO_SEND:
                                bs = recvPdu[FC_BS_INDEX]
                                if(bs == 0):
                                    bs = 585
                                blockList = self.create_blockList(payload[payloadPtr:], bs)
                                stMin = self.decode_stMin(recvPdu[FC_STMIN_INDEX])
                                currBlock = blockList.pop(0)
                                state = CanTpState.SEND_CONSECUTIVE_FRAME
                                stMinTimer.timeoutTime = stMin
                                stMinTimer.start()
                                timeoutTimer.stop()
                        else:
                            raise Exception("Unexpected Flow Control Continue to Send request")
                    else:
                        raise Exception("Unexpected fs response from ECU")
                else:
                    raise Exception("Unexpected response from device")

            if state == CanTpState.SEND_SINGLE_FRAME:
                txPdu[N_PCI_INDEX] = (CanTpMessageType.SINGLE_FRAME << 4) + payloadLength
                txPdu[SINGLE_FRAME_DATA_START_INDEX:] = fillArray(payload, self.__maxPduLength)
                self.transmit(txPdu)
                endOfMessage_flag = True
            elif state == CanTpState.SEND_FIRST_FRAME:
                payloadLength_highNibble = (payloadLength & 0xF00) >> 8
                payloadLength_lowNibble  = (payloadLength & 0x0FF)
                txPdu[N_PCI_INDEX] = (CanTpMessageType.FIRST_FRAME << 4)
                txPdu[FIRST_FRAME_DL_INDEX_HIGH] += payloadLength_highNibble
                txPdu[FIRST_FRAME_DL_INDEX_LOW] = payloadLength_lowNibble
                txPdu[FIRST_FRAME_DATA_START_INDEX:] = payload[0:self.__maxPduLength-1]
                payloadPtr = self.__maxPduLength-1
                self.transmit(txPdu)
                timeoutTimer.start()
                state = CanTpState.WAIT_FLOW_CONTROL
            elif state == CanTpState.SEND_CONSECUTIVE_FRAME:
                if(stMinTimer.isExpired()):
                    cfTiming.append(perf_counter())
                    txPdu[0] = (CanTpMessageType.CONSECUTIVE_FRAME << 4) + sequenceNumber
                    txPdu[1:] = currBlock.pop(0)
                    self.transmit(txPdu)
                    sequenceNumber = (sequenceNumber + 1) % 16
                    stMinTimer.restart()
                    if(len(currBlock) == 0):
                        if(len(blockList) == 0):
                            endOfMessage_flag = True
                        else:
                            timeoutTimer.start()
                            state = CanTpState.WAIT_FLOW_CONTROL

            # timer / exit condition checks
            if(timeoutTimer.isExpired()):
                raise Exception("Timeout waiting for message")

    ##
    # @brief recv method
    # @param [in] timeout_ms The timeout to wait before exiting
    # @return a list
    def recv(self, timeout_s):

        timeoutTimer = ResettableTimer(timeout_s)

        payload = []
        payloadPtr = 0
        payloadLength = None

        sequenceNumberExpected = 0

        txData = [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]

        endOfMessage_flag = False

        state = CanTpState.IDLE

        timeoutTimer.start()
        while endOfMessage_flag is False:

            recvPdu = self.getNextBufferedMessage()

            if recvPdu is not None:
                N_PCI = (recvPdu[N_PCI_INDEX] & 0xF0) >> 4
                if state == CanTpState.IDLE:
                    if N_PCI == CanTpMessageType.SINGLE_FRAME:
                        payloadLength = recvPdu[N_PCI_INDEX & 0x0F]
                        payload = recvPdu[SINGLE_FRAME_DATA_START_INDEX: SINGLE_FRAME_DATA_START_INDEX + payloadLength]
                        endOfMessage_flag = True
                    elif N_PCI == CanTpMessageType.FIRST_FRAME:
                        payload = recvPdu[FIRST_FRAME_DATA_START_INDEX:]
                        payloadLength = ((recvPdu[FIRST_FRAME_DL_INDEX_HIGH] & 0x0F) << 8) + recvPdu[FIRST_FRAME_DL_INDEX_LOW]
                        payloadPtr = self.__maxPduLength - 1
                        state = CanTpState.SEND_FLOW_CONTROL
                elif state == CanTpState.RECEIVING_CONSECUTIVE_FRAME:
                    if N_PCI == CanTpMessageType.CONSECUTIVE_FRAME:
                        sequenceNumber = recvPdu[CONSECUTIVE_FRAME_SEQUENCE_NUMBER_INDEX] & 0x0F
                        if sequenceNumber != sequenceNumberExpected:
                            raise Exception("Consecutive frame sequence out of order")
                        else:
                            sequenceNumberExpected = (sequenceNumberExpected + 1) % 16
                        payload += recvPdu[CONSECUTIVE_FRAME_SEQUENCE_DATA_START_INDEX:]
                        payloadPtr += (self.__maxPduLength)
                    else:
                        raise Exception("Unexpected PDU received")

            if state == CanTpState.SEND_FLOW_CONTROL:
                txData[N_PCI_INDEX] = 0x30
                txData[FLOW_CONTROL_BS_INDEX] = 0
                txData[FLOW_CONTROL_STMIN_INDEX] = 0x1E
                self.transmit(txData)
                state = CanTpState.RECEIVING_CONSECUTIVE_FRAME

            if payloadLength is not None:
                if payloadPtr >= payloadLength:
                    endOfMessage_flag = True

            if timeoutTimer.isExpired():
                raise Exception("Timeout in waiting for message")

        return list(payload[:payloadLength])

    ##
    # @brief clear out the receive list
    def clearBufferedMessages(self):
        self.__recvBuffer = []

    ##
    # @brief retrieves the next message from the received message buffers
    # @return list, or None if nothing is on the receive list
    def getNextBufferedMessage(self):
        length = len(self.__recvBuffer)
        if(length != 0):
            return self.__recvBuffer.pop(0)
        else:
            return None

    ##
    # @brief the listener callback used when a message is received
    def callback_onReceive(self, msg):
        if(msg.arbitration_id == self.__resId):
            # print("CanTp Instance received message")
            # print(unpack('BBBBBBBB', msg.data))
            self.__recvBuffer.append(msg.data[self.__pduStartIndex:])

    ##
    # @brief function to decode the StMin parameter
    @staticmethod
    def decode_stMin(val):
        if (val <= 0x7F):
            time = val / 1000
            return time
        elif (
                (val >= 0xF1) &
                (val <= 0xF9)
        ):
            time = (val & 0x0F) / 10000
            return time
        else:
            raise Exception("Unknown STMin time")

    def create_blockList(self, payload, blockSize):

        blockList = []
        currBlock = []
        currPdu = []

        payloadPtr = 0
        blockPtr = 0

        payloadLength = len(payload)
        pduLength = self.__maxPduLength
        blockLength = blockSize * pduLength

        working = True
        while(working):
            if (payloadPtr + pduLength) >= payloadLength:
                working = False
                currPdu = fillArray(payload[payloadPtr:], pduLength)
                currBlock.append(currPdu)
                blockList.append(currBlock)

            if working:
                currPdu = payload[payloadPtr:payloadPtr+pduLength]
                currBlock.append(currPdu)
                payloadPtr += pduLength
                blockPtr += pduLength

                if(blockPtr == blockLength):
                    blockList.append(currBlock)
                    currBlock = []
                    blockPtr = 0

        return blockList

    def transmit(self, data):

        canMsg = can.Message(arbitration_id=self.__reqId)
        canMsg.data = [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]
        if self.__addressingType == CanTpAddressingTypes.NORMAL_FIXED:
            canMsg.data = data
        else:
            canMsg.data[0] = 0xFF
            canMsg.data[1:] = data
        self.__bus.send(canMsg)


if __name__ == "__main__":
    pass