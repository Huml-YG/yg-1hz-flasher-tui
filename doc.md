# Kominikace přes port pro teploměr

## defaultní nastavení portu
- 300 baud
- 8bit bez parity

## příkazy
- příkazy jsou posílany v ASCII
- parametry končí čárkou
- i za posledním parametrem je čárka
- příkazy jsou oddělené koncem řádku ('\n' nebo '\r' nebo obojí)
- před prvním příkazem je třeba poslat konec řádku
- na příkazy zařízení odpovídá

### teplota
standardní příkaz co posílá teploměr

`\r\n121,0244,`
- 121 - ignorováno (vlhkost pokud senzor umí vlhkost)
- 0244 - teplota - 24.4C

bez odpovědi

### kalibrace měření
`\r\nTEST,CALIB,1,231500,760,`
- 1 - kanál 1 na současném hw vždy 1
- 231500 - aktuální napětí v mV - 231.5V
- 760 - aktuální proud v mA - 0.76A

odpověď `RESP,OK,\r\n`

### nastavení zpoždění relé
`\r\nTEST,RELAY,1,555,777,`
- 1 - kanál 1 na současném hw vždy 1
- 555 zpoždění při spínání v us
- 777 zpoždění při rozepínání v us

odpověď `RESP,OK,\r\n`

### vyčtění měření

`\r\nTEST,MEAS,1,`
- 1 - kanál 1 na současném hw vždy 1

odpověď `RESP,232500,700,\r\n`
- 232500 - aktuální napětí v 1 mV - 232.5V
- 700 - aktuální proud v mA - 0.7A

### info z modemu

`\r\nTEST,MODEM,`

odpověď `RESP,490154203237518,111811911121131411,\r\n`
- 490154203237518 - IMEI může být prázdné pokud modem nenaběhl nebo nefunguje
- 111811911121131411 - ICCID (číslo sim) může být prázdné může být prázdné pokud modem nenaběhl nebo nefunguje nebo není vložena sim

### přepnutí výsupu
`\r\nTEST,OUT,1,0,`
- 1 - kanál 1 na současném hw vždy 1
- 0 - vypnout (1 - zapnout)


odpověď `RESP,OK,\r\n`

### změna přenosové rychlosti

`\r\nTEST,BAUD,9600,`

- 9600 - rychlost debug portu v baudech

odpověď `RESP,OK,\r\n` - už novou rychlostí

### Nastavení mqtt

`\r\nTEST,MQTT,example.com,8883,1,user1,hunter2,`

- example.com - mqtt host
- 8883 - port
- 1 - ssl enabled (0 for disabled)
- user1 - username (also part of topics)
- hunter2 - password

odpověď `RESP,OK,\r\n`


