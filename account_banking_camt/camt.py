from lxml import etree
from datetime import datetime
import re
from account_banking.parsers import models
from account_banking.parsers.convert import str2date
from account_banking.sepa import postalcode
from tools.translate import _

bt = models.mem_bank_transaction

class transaction(models.mem_bank_transaction):

    def __init__(self, values, *args, **kwargs):
        super(transaction, self).__init__(*args, **kwargs)
        for attr in values:
                setattr(self, attr, values[attr])

class parser(models.parser):
    code = 'CAMT'
    country_code = 'NL'
    name = _('Generic CAMT Format')
    doc = _('''\
CAMT Format parser
''')

    def tag(self, node):
        """
        Return the tag of a node, stripped from its namespace
        """
        return node.tag[len(self.ns):]

    def assert_tag(self, node, expected):
        """
        Get node's stripped tag and compare with expected
        """
        assert self.tag(node) == expected, (
            _("Expected tag '%s', got '%s' instead") %
            (self.tag(node), expected))
        
    def xpath(self, node, expr):
        """
        Wrap namespaces argument into call to Element.xpath():

        self.xpath(node, './ns:Acct/ns:Id')
        """
        return node.xpath(expr, namespaces={'ns': self.ns[1:-1]})

    def find(self, node, expr):
        """
        Like xpath(), but return first result if any or else False
        
        Return None to test nodes for being truesy
        """
        result = node.xpath(expr, namespaces={'ns': self.ns[1:-1]})
        if result:
            return result[0]
        return None

    def get_balance_type_node(self, node, balance_type):
        """
        :param node: BkToCstmrStmt/Stmt/Bal node
        :param balance type: one of 'OPBD', 'PRCD', 'ITBD', 'CLBD'
        """
        code_expr = './ns:Bal/ns:Tp/ns:CdOrPrtry/ns:Cd[text()="%s"]/../../..' % balance_type
        return self.xpath(node, code_expr)
    
    def parse_amount(self, node):
        """
        Parse an element that contains both Amount and CreditDebitIndicator
        
        :return: signed amount
        :returntype: float
        """
        sign = -1 if node.find(self.ns + 'CdtDbtInd').text == 'CRDT' else 1
        return sign * float(node.find(self.ns + 'Amt').text)
        
    def get_start_balance(self, node):
        """
        Find the (only) balance node with code OpeningBalance, or
        the only one with code 'PreviousClosingBalance'
        or the first balance node with code InterimBalance in
        the case of preceeding pagination.

        :param node: BkToCstmrStmt/Stmt/Bal node
        """
        nodes = (
            self.get_balance_type_node(node, 'OPBD') or
            self.get_balance_type_node(node, 'PRCD') or
            self.get_balance_type_node(node, 'ITBD'))
        return self.parse_amount(nodes[0])

    def get_end_balance(self, node):
        """
        Find the (only) balance node with code ClosingBalance, or
        the second (and last) balance node with code InterimBalance in
        the case of continued pagination.

        :param node: BkToCstmrStmt/Stmt/Bal node
        """
        nodes = (
            self.get_balance_type_node(node, 'CLBD') or
            self.get_balance_type_node(node, 'ITBD'))
        return self.parse_amount(nodes[-1])

    def parse_Stmt(self, node):
        statement = models.mem_bank_statement()
        statement.id = node.find(self.ns + 'Id').text
        statement.local_account = (
            self.xpath(node, './ns:Acct/ns:Id/ns:IBAN')[0].text
            if self.xpath(node, './ns:Acct/ns:Id/ns:IBAN')
            else self.xpath(node, './ns:Acct/ns:Id/ns:Othr/ns:Id')[0].text)
        statement.local_currency = self.xpath(node, './ns:Acct/ns:Ccy')[0].text
        statement.start_balance = self.get_start_balance(node)
        statement.end_balance = self.get_end_balance(node)
        print "Number of Ntry in statement: %s" % len(self.xpath(node, './ns:Ntry'))
        for Ntry in self.xpath(node, './ns:Ntry'):
            for transaction_detail in self.parse_Ntry(Ntry):
                statement.transactions.append(
                    transaction(transaction_detail))
        return statement

    def get_entry_description(self, node):
        """
        :param node: Ntry node
        """
        codes = self.xpath(node, './ns:BkTxCd/ns:Prtry/ns:Cd')
        if codes:
            return codes[0].text
        return False

    def parse_Ntry(self, node):
        entry_description = self.get_entry_description(node)
        entry_details = {
            'effective_date': self.xpath(node, './ns:BookgDt/ns:Dt')[0].text,
            'transaction_date': self.xpath(node, './ns:ValDt/ns:Dt')[0].text,
            'transfer_type': bt.ORDER,
            'transferred_amount': self.parse_amount(node)
            }
        amount_sign = -1 if node.find(self.ns + 'CdtDbtInd').text == 'CRDT' else 1
        transaction_details = []
        print "  NUmber of NtryDtls in Ntry with code %s: %s" % (
            entry_description, len(self.xpath(node, './ns:NtryDtls')))
        for NtryDtl in self.xpath(node, './ns:NtryDtls'):
            TxDtls = self.xpath(NtryDtl, './ns:TxDtls')
            # Todo: process Btch tag on entry-detail level
            print "    NUmber of TxDtls in NtryDtl: %s" % len(TxDtls)
            if len(TxDtls) == 1:
                vals = self.parse_TxDtl(TxDtls[0], entry_details, amount_sign)
            else:
                vals = entry_details
            print vals
            transaction_details.append(transaction(vals))
        return transaction_details

    def get_party_values(self, TxDtl):
        """
        Determine to get either the debtor or creditor party node
        and extract the available data from it
        """
        vals = {}
        party_type = self.find(
            TxDtl, '../../ns:CdtDbtInd').text == 'CRDT' and 'Dbtr' or 'Cdtr'
        party_node = self.find(TxDtl, './ns:RltdPties/ns:%s' % party_type)
        account_node = self.find(TxDtl, './ns:RltdPties/ns:%sAcct/ns:Id' % party_type)
        bic_node = self.find(
            TxDtl,
            './ns:RltdAgts/ns:%sAgt/ns:FinInstnId/ns:BIC' % party_type)
        if party_node is not None:
            name_node = self.find(party_node, './ns:Nm')
            vals['remote_owner'] = name_node.text if name_node is not None else False
            country_node = self.find(party_node, './ns:PstlAdr/ns:Ctry')
            vals['remote_owner_country'] = (
                country_node.text if country_node is not None else False)
            address_node = self.find(party_node, './ns:AdrLine')
            vals['remote_owner_address'] = (
                address_node.text if address_node is not None else False)
        if account_node is not None:
            iban_node = self.find(account_node, './ns:IBAN')
            if iban_node is not None:
                vals['remote_account'] = iban_node.text
                if bic_node is not None:
                    vals['remote_iban'] = bic_node.text
            else:
                domestic_node = self.find(account_node, './ns:Othr/ns:Id')
                vals['remote_account'] = (
                    domestic_node.text if domestic_node is not None else False)
        return vals

    def parse_TxDtl(self, TxDtl, entry_values, amount_sign):
        vals = dict(entry_values)
        # amount = amount_sign * float(node.find(self.ns + 'Amt').text)
        unstructured = self.xpath(TxDtl, './ns:RmtInf/ns:Ustrd')
        if unstructured:
            vals['message'] = ' '.join([x.text for x in unstructured])
        structured = self.find(TxDtl, './ns:RmtInf/ns:Strd/ns:CdtrRefInf/ns:Ref')
        if structured is not None:
            vals['reference'] = structured.text
        else:
            if vals['message'] and re.match('^[^\s]$', vals['message']):
                vals['reference'] = vals['message']
        vals.update(self.get_party_values(TxDtl))
        return vals

    def parse(self, cr, data):
        root = etree.fromstring(data)
        self.ns = root.tag[:root.tag.index("}") + 1]
        self.assert_tag(root[0][0], 'GrpHdr')
        statements = []
        for node in root[0][1:]:
            statements.append(self.parse_Stmt(node))
        return statements
