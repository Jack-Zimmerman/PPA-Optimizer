// 8-bit registered ALU written in SystemVerilog (logic, always_ff/always_comb, enum).
module alu_f16 #(parameter int W = 16) (
    input  logic         clk,
    input  logic         rst,
    input  logic [W-1:0] a,
    input  logic [W-1:0] b,
    input  logic [2:0]   op,
    output logic [W-1:0] y,
    output logic         lt
);
    typedef enum logic [2:0] {ADD, SUB, AND, OR, XOR, SHL, SHR} op_e;
    logic [W-1:0] y_next;

    always_comb begin
        unique case (op_e'(op))
            ADD:     y_next = a + b;
            SUB:     y_next = a - b;
            AND:     y_next = a & b;
            OR:      y_next = a | b;
            XOR:     y_next = a ^ b;
            SHL:     y_next = a << b[2:0];
            SHR:     y_next = a >> b[2:0];
            default: y_next = '0;
        endcase
    end

    always_ff @(posedge clk) begin
        if (rst) begin y <= '0; lt <= 1'b0; end
        else     begin y <= y_next; lt <= (a < b); end
    end
endmodule
